from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers.publishing import (
    PublishInvocationEvidence,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    PublishTarget,
    PublishingProviderHealth,
    PublishingResponseError,
    approve_publish_request,
    publish_artifact_ref,
    publish_idempotency_key,
    semantic_publish_request_digest,
)
from content_forge.providers.publishing_validation import validate_publish_result


def _approved(*, scheduled_for: datetime | None = None):
    manifest = RenderArtifactManifest(
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
        bytes_written=1234,
        elapsed_seconds=1.0,
        width=1080,
        height=1920,
        duration_seconds=8.0,
        fps=30.0,
        has_audio=True,
    )
    request = PublishRequest(
        artifact=publish_artifact_ref(manifest),
        target=PublishTarget(provider_id="fixture", destination_id="channel-main"),
        metadata=PublishMetadata(
            title="Approved title",
            visibility="private",
            scheduled_for=scheduled_for,
        ),
    )
    return approve_publish_request(request)


def _health() -> PublishingProviderHealth:
    return PublishingProviderHealth(
        provider_id="fixture",
        provider_version="1",
        available=True,
    )


def _result(
    approved,
    *,
    disposition: str = "published",
    effective_at: datetime | None = None,
    **evidence_updates,
) -> PublishResult:
    evidence = PublishInvocationEvidence(
        provider_id="fixture",
        provider_version="1",
        request_sha256=semantic_publish_request_digest(approved.request),
        idempotency_key=publish_idempotency_key(approved.request),
        output_sha256=approved.request.artifact.output_sha256,
        destination_id=approved.request.target.destination_id,
    ).model_copy(update=evidence_updates)
    return PublishResult(
        disposition=disposition,
        remote_id="remote-1",
        remote_url="https://example.invalid/watch/remote-1",
        effective_at=(
            datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)
            if effective_at is None
            else effective_at
        ),
        evidence=evidence,
    )


def test_pr27_validate_publish_result_accepts_exact_evidence() -> None:
    approved = _approved()
    result = _result(approved)
    assert validate_publish_result(approved, _health(), result) is result


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("provider_id", "other", "provider identity"),
        ("provider_version", "2", "provider version"),
        ("request_sha256", "a" * 64, "request digest"),
        ("idempotency_key", f"cfp-{'d' * 64}", "idempotency key"),
        ("output_sha256", "b" * 64, "output digest"),
        ("destination_id", "other-channel", "destination"),
    ],
)
def test_pr27_validate_publish_result_rejects_mismatched_evidence(
    field: str,
    value: str,
    message: str,
) -> None:
    approved = _approved()
    with pytest.raises(PublishingResponseError, match=message):
        validate_publish_result(approved, _health(), _result(approved, **{field: value}))


def test_pr27_validate_publish_result_rejects_wrong_or_unavailable_provider() -> None:
    approved = _approved()
    result = _result(approved)
    with pytest.raises(PublishingResponseError, match="target provider"):
        validate_publish_result(
            approved,
            PublishingProviderHealth(provider_id="other", provider_version="1", available=True),
            result,
        )
    with pytest.raises(PublishingResponseError, match="unavailable"):
        validate_publish_result(
            approved,
            PublishingProviderHealth(provider_id="fixture", provider_version="1", available=False),
            result,
        )


def test_pr27_unscheduled_request_requires_published_result() -> None:
    approved = _approved()
    with pytest.raises(PublishingResponseError, match="must be published"):
        validate_publish_result(
            approved,
            _health(),
            _result(approved, disposition="scheduled"),
        )


def test_pr27_scheduled_request_requires_scheduled_result_and_exact_instant() -> None:
    scheduled_for = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    approved = _approved(scheduled_for=scheduled_for)
    scheduled = _result(
        approved,
        disposition="scheduled",
        effective_at=scheduled_for,
    )
    assert validate_publish_result(approved, _health(), scheduled) is scheduled

    with pytest.raises(PublishingResponseError, match="must be scheduled"):
        validate_publish_result(
            approved,
            _health(),
            _result(approved, disposition="published", effective_at=scheduled_for),
        )

    with pytest.raises(PublishingResponseError, match="effective time"):
        validate_publish_result(
            approved,
            _health(),
            _result(
                approved,
                disposition="scheduled",
                effective_at=datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc),
            ),
        )
