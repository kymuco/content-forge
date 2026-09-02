from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from content_forge.application.publishing import PublishingService
from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers import (
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    PublishingExecutionError,
    PublishingProviderHealth,
    PublishingResponseError,
    approve_publish_request,
    publish_artifact_ref,
    publish_idempotency_key,
)
from content_forge.providers.youtube import (
    YouTubePublishingConfig,
    YouTubePublishingProvider,
    _youtube_tag_budget,
)
from content_forge.providers.youtube_auth import write_private_token
from content_forge.storage import LocalLibrary


CHANNEL_ID = "UC1234567890123456789012"


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


def _approved(
    artifact: RenderArtifactManifest,
    *,
    visibility: str = "private",
    scheduled_for: datetime | None = None,
    title: str = "Approved upload",
    description: str = "description",
    tags: tuple[str, ...] = ("Genshin", "Raiden Shogun"),
):
    request = PublishRequest(
        artifact=publish_artifact_ref(artifact),
        target=PublishTarget(provider_id="youtube", destination_id=CHANNEL_ID),
        metadata=PublishMetadata(
            title=title,
            description=description,
            tags=tags,
            visibility=visibility,
            scheduled_for=scheduled_for,
        ),
    )
    return approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc),
    )


class _ExecuteRequest:
    def __init__(self, payload):
        self.payload = payload
        self.retries = None

    def execute(self, *, num_retries=0):
        self.retries = num_retries
        return self.payload


class _UploadRequest:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.retries = []

    def next_chunk(self, *, num_retries=0):
        self.calls += 1
        self.retries.append(num_retries)
        return None, self.response


class _Channels:
    def __init__(self, channel_id=CHANNEL_ID):
        self.channel_id = channel_id
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _ExecuteRequest({"items": [{"id": self.channel_id}]})


class _Videos:
    def __init__(self, *, verify_payload):
        self.verify_payload = verify_payload
        self.insert_calls = []
        self.list_calls = []
        self.upload_request = _UploadRequest({"id": "video_123"})

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        return self.upload_request

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _ExecuteRequest(self.verify_payload)


class _Service:
    def __init__(self, *, channel_id=CHANNEL_ID, verify_payload=None):
        self.channels_api = _Channels(channel_id)
        if verify_payload is None:
            verify_payload = {
                "items": [
                    {
                        "id": "video_123",
                        "snippet": {
                            "channelId": CHANNEL_ID,
                            "publishedAt": "2026-09-02T07:05:00Z",
                        },
                        "status": {"privacyStatus": "private"},
                    }
                ]
            }
        self.videos_api = _Videos(verify_payload=verify_payload)

    def channels(self):
        return self.channels_api

    def videos(self):
        return self.videos_api


class _ArtifactLoader:
    def __init__(self, artifact):
        self.artifact = artifact

    def load_artifact(self, job_id: str, *, ffprobe_path: str, probe_timeout: float):
        assert job_id == self.artifact.job_id
        return self.artifact


def _provider(
    tmp_path: Path,
    service: _Service,
    *,
    now: datetime = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc),
):
    token_path = tmp_path / "youtube-token.json"
    token_path.write_text("{}", encoding="utf-8")
    if os.name != "nt":
        os.chmod(token_path, 0o600)
    return YouTubePublishingProvider(
        YouTubePublishingConfig(
            token_path=str(token_path),
            channel_id=CHANNEL_ID,
            max_retries=4,
        ),
        credentials_loader=lambda path: object(),
        service_factory=lambda credentials: service,
        media_upload_factory=lambda path: {"path": str(path), "resumable": True},
        clock=lambda: now,
    )


def test_pr28_youtube_tag_budget_matches_documented_space_and_comma_accounting() -> None:
    assert _youtube_tag_budget(("alpha", "two words")) == 5 + 1 + 9 + 2


def test_pr28_youtube_health_pins_exact_configured_channel(tmp_path: Path) -> None:
    service = _Service()
    provider = _provider(tmp_path, service)
    health = provider.health()

    assert health == PublishingProviderHealth(
        provider_id="youtube",
        provider_version="youtube_data_api_v3_pr28_v1",
        available=True,
        reason=None,
    )
    assert service.channels_api.calls == [{"part": "id", "mine": True, "maxResults": 2}]

    mismatch = _provider(tmp_path, _Service(channel_id="UC_OTHER"))
    mismatch_health = mismatch.health()
    assert mismatch_health.available is False
    assert mismatch_health.provider_id == "youtube"
    assert "configured destination" in (mismatch_health.reason or "")


def test_pr28_youtube_preflight_enforces_platform_metadata_before_remote_boundary(tmp_path: Path) -> None:
    artifact = _artifact()
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"test")
    provider = _provider(tmp_path, _Service())
    now = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)

    valid = _approved(artifact, visibility="public")
    provider.preflight(
        valid,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(valid.request),
    )

    too_long = _approved(artifact, title="x" * 101)
    with pytest.raises(PublishingExecutionError, match="at most 100"):
        provider.preflight(
            too_long,
            media_path=media_path,
            idempotency_key=publish_idempotency_key(too_long.request),
        )

    too_many_description_bytes = _approved(artifact, description="Ж" * 2501)
    with pytest.raises(PublishingExecutionError, match="5000 UTF-8 bytes"):
        provider.preflight(
            too_many_description_bytes,
            media_path=media_path,
            idempotency_key=publish_idempotency_key(too_many_description_bytes.request),
        )

    scheduled_private = _approved(
        artifact,
        visibility="private",
        scheduled_for=now + timedelta(hours=1),
    )
    with pytest.raises(PublishingExecutionError, match="requires approved public visibility"):
        provider.preflight(
            scheduled_private,
            media_path=media_path,
            idempotency_key=publish_idempotency_key(scheduled_private.request),
        )

    past = _approved(
        artifact,
        visibility="public",
        scheduled_for=now - timedelta(seconds=1),
    )
    with pytest.raises(PublishingExecutionError, match="must be in the future"):
        provider.preflight(
            past,
            media_path=media_path,
            idempotency_key=publish_idempotency_key(past.request),
        )


def test_pr28_youtube_unscheduled_upload_uses_resumable_insert_and_verifies_video(tmp_path: Path) -> None:
    artifact = _artifact()
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"test")
    service = _Service(
        verify_payload={
            "items": [
                {
                    "id": "video_123",
                    "snippet": {
                        "channelId": CHANNEL_ID,
                        "publishedAt": "2026-09-02T07:05:00Z",
                    },
                    "status": {"privacyStatus": "unlisted"},
                }
            ]
        }
    )
    provider = _provider(tmp_path, service)
    approved = _approved(artifact, visibility="unlisted")

    assert provider.health().available is True
    provider.preflight(
        approved,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(approved.request),
    )
    result = provider.publish(
        approved,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(approved.request),
    )

    assert service.videos_api.insert_calls[0]["part"] == "snippet,status"
    assert service.videos_api.insert_calls[0]["body"] == {
        "snippet": {
            "title": "Approved upload",
            "description": "description",
            "tags": ["Genshin", "Raiden Shogun"],
        },
        "status": {"privacyStatus": "unlisted"},
    }
    assert service.videos_api.upload_request.calls == 1
    assert service.videos_api.upload_request.retries == [4]
    assert service.videos_api.list_calls == [
        {"part": "snippet,status", "id": "video_123", "maxResults": 1}
    ]
    assert result.disposition == "published"
    assert result.remote_id == "video_123"
    assert result.remote_url == "https://www.youtube.com/watch/video_123"
    assert result.effective_at == datetime(2026, 9, 2, 7, 5, tzinfo=timezone.utc)


def test_pr28_youtube_schedule_maps_public_approval_to_private_publish_at(tmp_path: Path) -> None:
    artifact = _artifact()
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"test")
    scheduled_for = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
    service = _Service(
        verify_payload={
            "items": [
                {
                    "id": "video_123",
                    "snippet": {
                        "channelId": CHANNEL_ID,
                        "publishedAt": "2026-09-02T07:05:00Z",
                    },
                    "status": {
                        "privacyStatus": "private",
                        "publishAt": "2026-09-02T08:00:00Z",
                    },
                }
            ]
        }
    )
    provider = _provider(tmp_path, service)
    approved = _approved(
        artifact,
        visibility="public",
        scheduled_for=scheduled_for,
    )

    assert provider.health().available is True
    provider.preflight(
        approved,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(approved.request),
    )
    result = provider.publish(
        approved,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(approved.request),
    )

    assert service.videos_api.insert_calls[0]["body"]["status"] == {
        "privacyStatus": "private",
        "publishAt": "2026-09-02T08:00:00Z",
    }
    assert result.disposition == "scheduled"
    assert result.effective_at == scheduled_for


def test_pr28_youtube_verification_mismatch_fails_closed_after_upload(tmp_path: Path) -> None:
    artifact = _artifact()
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"test")
    service = _Service(
        verify_payload={
            "items": [
                {
                    "id": "video_123",
                    "snippet": {
                        "channelId": "UC_WRONG",
                        "publishedAt": "2026-09-02T07:05:00Z",
                    },
                    "status": {"privacyStatus": "private"},
                }
            ]
        }
    )
    provider = _provider(tmp_path, service)
    approved = _approved(artifact)

    assert provider.health().available is True
    provider.preflight(
        approved,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(approved.request),
    )
    with pytest.raises(PublishingResponseError, match="different channel"):
        provider.publish(
            approved,
            media_path=media_path,
            idempotency_key=publish_idempotency_key(approved.request),
        )


def test_pr28_service_provider_preflight_failure_stays_retryable_failed(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path)
    artifact = _artifact()
    media_path = library.paths.root / artifact.output_storage_key
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"test")
    approved = _approved(artifact, title="x" * 101)
    provider = _provider(tmp_path, _Service())
    service = PublishingService(
        library,
        provider,
        render_orchestrator=_ArtifactLoader(artifact),
    )

    attempt = service.prepare(approved)
    with pytest.raises(PublishingExecutionError, match="provider preflight failed"):
        service.execute_prepared(attempt.attempt_id)

    stored = library.publishing.get_attempt(attempt.attempt_id)
    assert stored is not None
    assert stored.state == "failed"
    assert stored.error_code == "provider_preflight_failed"
    assert stored.provider_health is None


def test_pr28_private_token_writer_is_atomic_and_owner_only(tmp_path: Path) -> None:
    token_path = tmp_path / "credentials" / "youtube-token.json"
    payload = json.dumps({"refresh_token": "TOP_SECRET"})
    write_private_token(token_path, payload)

    assert json.loads(token_path.read_text(encoding="utf-8"))["refresh_token"] == "TOP_SECRET"
    assert not list(token_path.parent.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
