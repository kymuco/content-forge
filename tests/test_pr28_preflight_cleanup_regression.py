from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import local

import pytest

from content_forge.application.publishing import PublishingService
from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers import (
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    PublishingProviderHealth,
    approve_publish_request,
    publish_artifact_ref,
)
from content_forge.providers.publishing import _PublishingPreflightCleanupProvider
from content_forge.providers.youtube import YouTubePublishingProvider, _PreparedUpload
from content_forge.storage import LocalLibrary, StorageConflictError

MEDIA_BYTES = b"test"
MEDIA_SHA256 = hashlib.sha256(MEDIA_BYTES).hexdigest()
CHANNEL_ID = "UC1234567890123456789012"


class _ArtifactLoader:
    def __init__(self, artifact: RenderArtifactManifest) -> None:
        self.artifact = artifact

    def load_artifact(self, job_id: str, *, ffprobe_path: str, probe_timeout: float):
        assert job_id == self.artifact.job_id
        return self.artifact


class _CleanupProvider:
    def __init__(self) -> None:
        self.preflight_active = False
        self.cleanup_calls = 0
        self.publish_calls = 0

    def health(self) -> PublishingProviderHealth:
        return PublishingProviderHealth(
            provider_id="youtube",
            provider_version="cleanup-regression-v1",
            available=True,
            reason=None,
        )

    def preflight(self, request, *, media_path: Path, idempotency_key: str) -> None:
        assert media_path.read_bytes() == MEDIA_BYTES
        assert idempotency_key.startswith("cfp-")
        self.preflight_active = True

    def _clear_execution_state(self) -> None:
        self.cleanup_calls += 1
        self.preflight_active = False

    def publish(self, request, *, media_path: Path, idempotency_key: str):
        self.publish_calls += 1
        raise AssertionError("publish must not run when mark_running loses the race")


def _artifact() -> RenderArtifactManifest:
    return RenderArtifactManifest(
        job_id=new_entity_id(EntityKind.JOB),
        project_id=new_entity_id(EntityKind.PROJECT),
        purpose="final",
        profile_id="youtube_shorts_1080p",
        render_plan_digest="1" * 64,
        command_manifest_digest="2" * 64,
        command_manifest_storage_key="commands/final.json",
        output_sha256=MEDIA_SHA256,
        output_storage_key="renders/final.mp4",
        manifest_storage_key="renders/final.manifest.json",
        video_encoder="libx264",
        ffmpeg_version="fixture",
        bytes_written=len(MEDIA_BYTES),
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
        target=PublishTarget(provider_id="youtube", destination_id=CHANNEL_ID),
        metadata=PublishMetadata(title="Cleanup regression", visibility="private"),
    )
    return approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc),
    )


def test_pr28_post_preflight_running_conflict_releases_provider_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path)
    artifact = _artifact()
    media = library.paths.root / artifact.output_storage_key
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(MEDIA_BYTES)

    provider = _CleanupProvider()
    service = PublishingService(
        library,
        provider,
        render_orchestrator=_ArtifactLoader(artifact),
    )
    attempt = service.prepare(_approved(artifact))
    repository = library.publishing

    def lose_running_transition(attempt_id: str, health: PublishingProviderHealth):
        raise StorageConflictError("concurrent execution already entered running")

    monkeypatch.setattr(repository, "mark_running", lose_running_transition)

    with pytest.raises(StorageConflictError, match="concurrent execution"):
        service.execute_prepared(attempt.attempt_id)

    assert provider.cleanup_calls == 1
    assert provider.preflight_active is False
    assert provider.publish_calls == 0
    stored = repository.get_attempt(attempt.attempt_id)
    assert stored is not None
    assert stored.state == "prepared"


def test_pr28_youtube_cleanup_capability_closes_prepared_snapshot() -> None:
    provider = object.__new__(YouTubePublishingProvider)
    provider._thread_state = local()
    snapshot = tempfile.TemporaryFile(mode="w+b")
    snapshot.write(MEDIA_BYTES)
    snapshot.seek(0)
    provider._thread_state.prepared = _PreparedUpload(
        service=object(),
        insert_request=object(),
        request_sha256="a" * 64,
        idempotency_key="cfp-" + "b" * 64,
        media_path=Path("fixture.mp4"),
        media_snapshot=snapshot,
    )

    assert isinstance(provider, _PublishingPreflightCleanupProvider)
    assert snapshot.closed is False

    provider._clear_execution_state()

    assert snapshot.closed is True
    assert not hasattr(provider._thread_state, "prepared")
