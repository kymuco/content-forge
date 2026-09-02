from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from content_forge.providers import (
    PublishArtifactRef,
    PublishDeclarations,
    PublishInvocationEvidence,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    PublishTarget,
    PublishingResponseError,
    approve_publish_request,
    publish_idempotency_key,
    semantic_publish_request_digest,
)
from content_forge.providers.youtube import (
    YouTubePublishingProvider as PR28YouTubePublishingProvider,
    _PreparedUpload,
)
from content_forge.providers.youtube_v2 import (
    YouTubePublishingConfig,
    YouTubePublishingProvider,
    _PROVIDER_VERSION,
    _verify_declarations,
    _youtube_body_with_declarations,
)

CHANNEL_ID = "UC1234567890123456789012"


def _request(*, v2: bool, child_directed: bool = False, synthetic: bool = False) -> PublishRequest:
    return PublishRequest(
        contract_version=(
            "pr29_publish_contract_v2" if v2 else "pr27_publish_contract_v1"
        ),
        artifact=PublishArtifactRef(
            project_id="cf_project_" + "1" * 32,
            render_job_id="cf_job_" + "2" * 32,
            profile_id="youtube_shorts_1080p",
            render_plan_digest="3" * 64,
            output_sha256="4" * 64,
            bytes_written=4,
            width=1080,
            height=1920,
            duration_seconds=4.0,
            has_audio=True,
        ),
        target=PublishTarget(provider_id="youtube", destination_id=CHANNEL_ID),
        metadata=PublishMetadata(
            title="Approved upload",
            description="description",
            tags=("one", "two"),
            visibility="private",
        ),
        declarations=(
            PublishDeclarations(
                child_directed=child_directed,
                contains_realistic_altered_or_synthetic_media=synthetic,
            )
            if v2
            else None
        ),
    )


def _approved(*, v2: bool, child_directed: bool = False, synthetic: bool = False):
    return approve_publish_request(
        _request(
            v2=v2,
            child_directed=child_directed,
            synthetic=synthetic,
        ),
        approved_at=datetime(2026, 9, 2, 9, 40, tzinfo=timezone.utc),
    )


def test_pr29_v1_youtube_body_remains_free_of_new_declaration_fields() -> None:
    body = _youtube_body_with_declarations(_approved(v2=False))
    status = body["status"]
    assert isinstance(status, dict)
    assert "selfDeclaredMadeForKids" not in status
    assert "containsSyntheticMedia" not in status


def test_pr29_v2_youtube_body_sets_both_explicit_boolean_declarations() -> None:
    false_body = _youtube_body_with_declarations(
        _approved(v2=True, child_directed=False, synthetic=False)
    )
    false_status = false_body["status"]
    assert isinstance(false_status, dict)
    assert false_status["selfDeclaredMadeForKids"] is False
    assert false_status["containsSyntheticMedia"] is False

    true_body = _youtube_body_with_declarations(
        _approved(v2=True, child_directed=True, synthetic=True)
    )
    true_status = true_body["status"]
    assert isinstance(true_status, dict)
    assert true_status["selfDeclaredMadeForKids"] is True
    assert true_status["containsSyntheticMedia"] is True


def test_pr29_youtube_remote_declarations_are_verified_exactly() -> None:
    approved = _approved(v2=True, child_directed=False, synthetic=True)
    _verify_declarations(
        {
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
        approved,
    )

    with pytest.raises(PublishingResponseError, match="made-for-kids"):
        _verify_declarations(
            {
                "selfDeclaredMadeForKids": True,
                "containsSyntheticMedia": True,
            },
            approved,
        )
    with pytest.raises(PublishingResponseError, match="altered/synthetic"):
        _verify_declarations(
            {
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": False,
            },
            approved,
        )
    with pytest.raises(PublishingResponseError, match="altered/synthetic"):
        _verify_declarations(
            {"selfDeclaredMadeForKids": False},
            approved,
        )


def test_pr29_youtube_publish_pins_v2_contract_and_provider_evidence(monkeypatch, tmp_path: Path) -> None:
    approved = _approved(v2=True, child_directed=False, synthetic=True)
    request = approved.request
    config = YouTubePublishingConfig(
        token_path=str((tmp_path / "token.json").resolve()),
        channel_id=CHANNEL_ID,
    )
    provider = YouTubePublishingProvider(config)
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"test")
    snapshot = io.BytesIO(b"test")
    service = object()
    provider._thread_state.prepared = _PreparedUpload(
        service=service,
        insert_request=object(),
        request_sha256=semantic_publish_request_digest(request),
        idempotency_key=publish_idempotency_key(request),
        media_path=media_path,
        media_snapshot=snapshot,
    )

    def fake_base_publish(self, approved_request, *, media_path, idempotency_key):
        return PublishResult(
            disposition="published",
            remote_id="video_123",
            remote_url="https://youtu.be/video_123",
            effective_at=datetime(2026, 9, 2, 9, 41, tzinfo=timezone.utc),
            evidence=PublishInvocationEvidence(
                provider_id="youtube",
                provider_version="youtube_data_api_v3_pr28_v1:category=22:notify=0",
                request_sha256=semantic_publish_request_digest(approved_request.request),
                idempotency_key=idempotency_key,
                output_sha256=approved_request.request.artifact.output_sha256,
                destination_id=approved_request.request.target.destination_id,
            ),
        )

    monkeypatch.setattr(PR28YouTubePublishingProvider, "publish", fake_base_publish)
    monkeypatch.setattr(
        "content_forge.providers.youtube_v2._verified_video",
        lambda resolved_service, video_id, *, retries: {
            "id": video_id,
            "status": {
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
            },
        },
    )

    result = provider.publish(
        approved,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(request),
    )
    assert result.evidence.contract_version == "pr29_publish_contract_v2"
    assert result.evidence.provider_version == _PROVIDER_VERSION


def test_pr29_package_provider_is_the_declaration_aware_extension() -> None:
    from content_forge.providers import YouTubePublishingProvider as ExportedProvider

    assert ExportedProvider is YouTubePublishingProvider
    assert issubclass(ExportedProvider, PR28YouTubePublishingProvider)
