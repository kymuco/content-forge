from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from content_forge.application.publishing import (
    PublishArtifactError,
    PublishOutcomeUnknownError,
    PublishingService,
)
from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers import (
    PublishInvocationEvidence,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    PublishTarget,
    PublishingExecutionError,
    PublishingProviderHealth,
    PublishingUnavailableError,
    approve_publish_request,
    publish_artifact_ref,
    publish_idempotency_key,
    semantic_publish_request_digest,
)
from content_forge.storage import LocalLibrary


class _ArtifactLoader:
    def __init__(self, artifact: RenderArtifactManifest | None, *, error: Exception | None = None) -> None:
        self.artifact = artifact
        self.error = error

    def load_artifact(self, job_id: str, *, ffprobe_path: str, probe_timeout: float):
        if self.error is not None:
            raise self.error
        if self.artifact is not None:
            assert job_id == self.artifact.job_id
        return self.artifact


def _artifact() -> RenderArtifactManifest:
    return RenderArtifactManifest(
        job_id=new_entity_id(EntityKind.JOB),
        project_id=new_entity_id(EntityKind.PROJECT),
        purpose="final",
        profile_id="youtube_shorts_1080p",
        render_plan_digest="1" * 64,
        command_manifest_digest="2" * 64,
        command_manifest_storage_key="commands/final.json",
        output_sha256="3" * 64,
        output_storage_key="renders/final.mp4",
        manifest_storage_key="renders/final.manifest.json",
        video_encoder="libx264",
        ffmpeg_version="fixture",
        bytes_written=4,
        elapsed_seconds=1.0,
        width=1080,
        height=1920,
        duration_seconds=8.0,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
    )


def _approved(artifact: RenderArtifactManifest):
    request = PublishRequest(
        artifact=publish_artifact_ref(artifact),
        target=PublishTarget(provider_id="fixture", destination_id="channel-main"),
        metadata=PublishMetadata(title="Approved upload", visibility="private"),
    )
    return approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc),
    )


def _prepare(tmp_path):
    library = LocalLibrary(tmp_path)
    artifact = _artifact()
    media_path = library.paths.root / artifact.output_storage_key
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"test")
    return library, artifact, _approved(artifact), media_path


class _SuccessfulProvider:
    def __init__(self, *, health_reason: str | None = "token=secret-health") -> None:
        self.health_reason = health_reason
        self.seen_media_path: Path | None = None
        self.seen_idempotency_key: str | None = None

    def health(self) -> PublishingProviderHealth:
        return PublishingProviderHealth(
            provider_id="fixture",
            provider_version="provider-v1",
            available=True,
            reason=self.health_reason,
        )

    def publish(self, request, *, media_path: Path, idempotency_key: str) -> PublishResult:
        self.seen_media_path = media_path
        self.seen_idempotency_key = idempotency_key
        return PublishResult(
            disposition="published",
            remote_id="remote-1",
            remote_url="https://example.invalid/watch/remote-1",
            effective_at=datetime(2026, 9, 2, 13, 5, tzinfo=timezone.utc),
            evidence=PublishInvocationEvidence(
                provider_id="fixture",
                provider_version="provider-v1",
                request_sha256=semantic_publish_request_digest(request.request),
                idempotency_key=idempotency_key,
                output_sha256=request.request.artifact.output_sha256,
                destination_id=request.request.target.destination_id,
            ),
        )


def test_pr27_publishing_service_success_uses_authenticated_artifact_and_redacted_health(tmp_path) -> None:
    library, artifact, approved, media_path = _prepare(tmp_path)
    provider = _SuccessfulProvider()
    service = PublishingService(
        library,
        provider,
        render_orchestrator=_ArtifactLoader(artifact),
    )

    attempt = service.publish(approved)
    assert attempt.state == "succeeded"
    assert attempt.provider_health is not None
    assert attempt.provider_health.provider_version == "provider-v1"
    assert attempt.provider_health.reason is None
    assert provider.seen_media_path == media_path
    assert provider.seen_idempotency_key == publish_idempotency_key(approved.request)
    stored = library.publishing.get_attempt(attempt.attempt_id)
    assert stored == attempt
    assert stored is not None and stored.provider_health is not None
    assert stored.provider_health.reason is None


def test_pr27_health_exception_is_known_preflight_failure_without_secret_persistence(tmp_path) -> None:
    library, artifact, approved, _ = _prepare(tmp_path)

    class Provider:
        def health(self):
            raise RuntimeError("Authorization: Bearer SUPER_SECRET")

        def publish(self, request, *, media_path: Path, idempotency_key: str):
            raise AssertionError("publish must not be called")

    service = PublishingService(library, Provider(), render_orchestrator=_ArtifactLoader(artifact))
    with pytest.raises(PublishingExecutionError, match="health check failed") as caught:
        service.publish(approved)
    assert "SUPER_SECRET" not in str(caught.value)

    attempts = library.publishing.attempts(approved.approval.request_sha256)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.state == "failed"
    assert attempt.error_code == "provider_health_failed"
    assert attempt.error_message == "publishing provider health check failed before remote execution"
    assert "SUPER_SECRET" not in attempt.model_dump_json()


def test_pr27_unavailable_health_reason_is_not_persisted_or_exposed(tmp_path) -> None:
    library, artifact, approved, _ = _prepare(tmp_path)

    class Provider:
        def health(self):
            return PublishingProviderHealth(
                provider_id="fixture",
                provider_version="provider-v1",
                available=False,
                reason="refresh_token=SUPER_SECRET",
            )

        def publish(self, request, *, media_path: Path, idempotency_key: str):
            raise AssertionError("publish must not be called")

    service = PublishingService(library, Provider(), render_orchestrator=_ArtifactLoader(artifact))
    with pytest.raises(PublishingUnavailableError, match="provider is unavailable") as caught:
        service.publish(approved)
    assert "SUPER_SECRET" not in str(caught.value)

    attempt = library.publishing.attempts(approved.approval.request_sha256)[0]
    assert attempt.state == "failed"
    assert attempt.error_code == "provider_unavailable"
    assert attempt.error_message == "publishing provider was unavailable before remote execution"
    assert "SUPER_SECRET" not in attempt.model_dump_json()


def test_pr27_remote_exception_becomes_unknown_without_secret_persistence(tmp_path) -> None:
    library, artifact, approved, _ = _prepare(tmp_path)

    class Provider(_SuccessfulProvider):
        def publish(self, request, *, media_path: Path, idempotency_key: str):
            raise RuntimeError("https://api.invalid/upload?access_token=SUPER_SECRET")

    service = PublishingService(library, Provider(), render_orchestrator=_ArtifactLoader(artifact))
    with pytest.raises(PublishOutcomeUnknownError, match="automatic retry is blocked") as caught:
        service.publish(approved)
    assert "SUPER_SECRET" not in str(caught.value)

    attempt = library.publishing.attempts(approved.approval.request_sha256)[0]
    assert attempt.state == "outcome_unknown"
    assert attempt.provider_health is not None and attempt.provider_health.reason is None
    assert attempt.error_code == "remote_outcome_unknown"
    assert attempt.error_message == "remote publish execution began but no authenticated outcome was recorded"
    assert "SUPER_SECRET" not in attempt.model_dump_json()

    with pytest.raises(Exception, match="unresolved remote outcome"):
        library.publishing.prepare_attempt(approved)


def test_pr27_result_mismatch_becomes_unknown_instead_of_retryable_failure(tmp_path) -> None:
    library, artifact, approved, _ = _prepare(tmp_path)

    class Provider(_SuccessfulProvider):
        def publish(self, request, *, media_path: Path, idempotency_key: str) -> PublishResult:
            result = super().publish(
                request,
                media_path=media_path,
                idempotency_key=idempotency_key,
            )
            return result.model_copy(
                update={
                    "evidence": result.evidence.model_copy(update={"output_sha256": "f" * 64})
                }
            )

    service = PublishingService(library, Provider(), render_orchestrator=_ArtifactLoader(artifact))
    with pytest.raises(PublishOutcomeUnknownError):
        service.publish(approved)
    attempt = library.publishing.attempts(approved.approval.request_sha256)[0]
    assert attempt.state == "outcome_unknown"


def test_pr27_keyboard_interrupt_leaves_running_until_restart_reconciliation(tmp_path) -> None:
    library, artifact, approved, _ = _prepare(tmp_path)

    class Provider(_SuccessfulProvider):
        def publish(self, request, *, media_path: Path, idempotency_key: str):
            raise KeyboardInterrupt()

    service = PublishingService(library, Provider(), render_orchestrator=_ArtifactLoader(artifact))
    with pytest.raises(KeyboardInterrupt):
        service.publish(approved)

    attempt = library.publishing.attempts(approved.approval.request_sha256)[0]
    assert attempt.state == "running"
    assert attempt.provider_health is not None and attempt.provider_health.reason is None

    assert service.reconcile_interrupted() == 1
    reconciled = library.publishing.get_attempt(attempt.attempt_id)
    assert reconciled is not None
    assert reconciled.state == "outcome_unknown"
    assert reconciled.error_code == "runtime_interrupted"
    assert "secret" not in reconciled.model_dump_json().lower()


def test_pr27_artifact_authentication_failure_creates_no_publish_operation(tmp_path) -> None:
    library, artifact, approved, _ = _prepare(tmp_path)
    service = PublishingService(
        library,
        _SuccessfulProvider(),
        render_orchestrator=_ArtifactLoader(None),
    )
    with pytest.raises(PublishArtifactError, match="no authenticated successful artifact"):
        service.publish(approved)

    # The publishing schema is still lazy until queried, and once opened there is no attempt.
    assert library.publishing.attempts(approved.approval.request_sha256) == ()
