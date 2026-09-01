from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers import (
    ApprovedPublishRequest,
    PublishApproval,
    PublishInvocationEvidence,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    PublishTarget,
    PublishingProvider,
    PublishingProviderHealth,
    approve_publish_request,
    publish_artifact_ref,
    semantic_publish_request_digest,
)


def _artifact(*, purpose: str = "final", output_sha256: str = "a" * 64) -> RenderArtifactManifest:
    return RenderArtifactManifest(
        job_id=new_entity_id(EntityKind.JOB),
        project_id=new_entity_id(EntityKind.PROJECT),
        purpose=purpose,
        profile_id="youtube_shorts_1080p",
        render_plan_digest="b" * 64,
        command_manifest_digest="c" * 64,
        command_manifest_storage_key="projects/p/commands/c.json",
        output_sha256=output_sha256,
        output_storage_key="projects/p/renders/final.mp4",
        manifest_storage_key="projects/p/renders/final.json",
        video_encoder="libx264",
        ffmpeg_version="ffmpeg fixture",
        bytes_written=123456,
        elapsed_seconds=1.25,
        width=1080,
        height=1920,
        duration_seconds=12.5,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
    )


def _request(*, title: str = "Exact title", destination_id: str = "channel-main") -> PublishRequest:
    return PublishRequest(
        artifact=publish_artifact_ref(_artifact()),
        target=PublishTarget(provider_id="fixture", destination_id=destination_id),
        metadata=PublishMetadata(
            title=title,
            description="description\nkeeps formatting",
            tags=("Genshin", "Raiden"),
            visibility="unlisted",
        ),
    )


def test_pr27_publish_artifact_ref_requires_final_render() -> None:
    final = _artifact()
    reference = publish_artifact_ref(final)
    assert reference.project_id == final.project_id
    assert reference.render_job_id == final.job_id
    assert reference.output_sha256 == final.output_sha256
    assert reference.render_plan_digest == final.render_plan_digest

    with pytest.raises(ValueError, match="only final render artifacts"):
        publish_artifact_ref(_artifact(purpose="preview"))


def test_pr27_publish_request_is_machine_independent_and_digest_is_exact() -> None:
    request = _request()
    encoded = request.model_dump_json()
    assert "media_path" not in encoded
    assert "token" not in encoded
    assert "credential" not in encoded

    digest = semantic_publish_request_digest(request)
    assert digest == semantic_publish_request_digest(request.model_copy(deep=True))
    assert digest != semantic_publish_request_digest(_request(title="Changed title"))
    assert digest != semantic_publish_request_digest(_request(destination_id="channel-secondary"))

    changed_artifact = request.model_copy(
        update={
            "artifact": request.artifact.model_copy(update={"output_sha256": "d" * 64})
        }
    )
    assert digest != semantic_publish_request_digest(changed_artifact)


def test_pr27_publish_schedule_is_canonicalized_to_utc_for_identity() -> None:
    instant_utc = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    instant_plus_six = instant_utc.astimezone(timezone(timedelta(hours=6)))
    base = _request()

    first = base.model_copy(
        update={"metadata": base.metadata.model_copy(update={"scheduled_for": instant_utc})}
    )
    second = PublishRequest(
        artifact=base.artifact,
        target=base.target,
        metadata=PublishMetadata(
            title=base.metadata.title,
            description=base.metadata.description,
            tags=base.metadata.tags,
            visibility=base.metadata.visibility,
            scheduled_for=instant_plus_six,
        ),
    )
    assert second.metadata.scheduled_for == instant_utc
    assert semantic_publish_request_digest(first) == semantic_publish_request_digest(second)


def test_pr27_approval_is_bound_to_exact_publish_request() -> None:
    request = _request()
    approved = approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
        note="human publish approval",
    )
    assert approved.approval.request_sha256 == semantic_publish_request_digest(request)

    changed = _request(title="Changed after approval")
    with pytest.raises(ValidationError, match="approval does not match"):
        ApprovedPublishRequest(request=changed, approval=approved.approval)

    forged = PublishApproval(
        request_sha256="0" * 64,
        approved_at=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(ValidationError, match="approval does not match"):
        ApprovedPublishRequest(request=request, approval=forged)


def test_pr27_publish_metadata_rejects_ambiguous_tags_and_naive_schedule() -> None:
    with pytest.raises(ValidationError, match="unique"):
        PublishMetadata(title="title", tags=("Raiden", "raiden"))

    with pytest.raises(ValidationError, match="timezone-aware"):
        PublishMetadata(title="title", scheduled_for=datetime(2026, 9, 3, 10, 0))


class _FixturePublishingProvider:
    def health(self) -> PublishingProviderHealth:
        return PublishingProviderHealth(
            provider_id="fixture",
            provider_version="1",
            available=True,
        )

    def publish(self, request: ApprovedPublishRequest, *, media_path: Path) -> PublishResult:
        assert media_path == Path("/runtime/final.mp4")
        digest = semantic_publish_request_digest(request.request)
        return PublishResult(
            disposition="published",
            remote_id="remote-123",
            remote_url="https://example.invalid/watch/remote-123",
            effective_at=datetime(2026, 9, 2, 12, 5, tzinfo=timezone.utc),
            evidence=PublishInvocationEvidence(
                provider_id="fixture",
                provider_version="1",
                request_sha256=digest,
                output_sha256=request.request.artifact.output_sha256,
                destination_id=request.request.target.destination_id,
            ),
        )


def test_pr27_provider_protocol_keeps_runtime_path_outside_approved_request() -> None:
    provider = _FixturePublishingProvider()
    assert isinstance(provider, PublishingProvider)
    approved = approve_publish_request(_request())
    result = provider.publish(approved, media_path=Path("/runtime/final.mp4"))
    assert result.evidence.request_sha256 == approved.approval.request_sha256
    assert result.evidence.output_sha256 == approved.request.artifact.output_sha256


def test_pr27_publish_result_rejects_secret_bearing_remote_url() -> None:
    with pytest.raises(ValidationError, match="without userinfo"):
        PublishResult(
            disposition="published",
            remote_id="remote-123",
            remote_url="https://user:secret@example.invalid/watch/remote-123",
            effective_at=datetime(2026, 9, 2, 12, 5, tzinfo=timezone.utc),
            evidence=PublishInvocationEvidence(
                provider_id="fixture",
                provider_version="1",
                request_sha256="a" * 64,
                output_sha256="b" * 64,
                destination_id="channel-main",
            ),
        )
