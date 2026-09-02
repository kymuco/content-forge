from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from content_forge.application.publishing import PublishAttemptError, PublishingService
from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers import (
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    PublishingPreflightError,
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
CATEGORY_ID = "22"
NOW = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def _artifact(*, duration_seconds: float = 8.0) -> RenderArtifactManifest:
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
        duration_seconds=duration_seconds,
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


def _verified_payload(
    *,
    privacy: str = "private",
    publish_at: str | None = None,
    channel_id: str = CHANNEL_ID,
    title: str = "Approved upload",
    description: str = "description",
    tags: tuple[str, ...] = ("Genshin", "Raiden Shogun"),
    category_id: str = CATEGORY_ID,
):
    status: dict[str, object] = {"privacyStatus": privacy}
    if publish_at is not None:
        status["publishAt"] = publish_at
    return {
        "items": [
            {
                "id": "video_123",
                "snippet": {
                    "channelId": channel_id,
                    "publishedAt": "2026-09-02T07:05:00Z",
                    "title": title,
                    "description": description,
                    "tags": list(tags),
                    "categoryId": category_id,
                },
                "status": status,
            }
        ]
    }


class _ExecuteRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self, *, num_retries=0):
        return self.payload


class _UploadRequest:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.retries: list[int] = []

    def next_chunk(self, *, num_retries=0):
        self.calls += 1
        self.retries.append(num_retries)
        return None, self.response


class _Channels:
    def __init__(self, channel_id=CHANNEL_ID, *, long_uploads_status: str = "allowed"):
        self.channel_id = channel_id
        self.long_uploads_status = long_uploads_status
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("part") == "status":
            return _ExecuteRequest(
                {
                    "items": [
                        {"status": {"longUploadsStatus": self.long_uploads_status}}
                    ]
                }
            )
        return _ExecuteRequest({"items": [{"id": self.channel_id}]})


class _VideoCategories:
    def __init__(
        self,
        *,
        category_id: str = CATEGORY_ID,
        assignable: bool = True,
    ) -> None:
        self.category_id = category_id
        self.assignable = assignable
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _ExecuteRequest(
            {
                "items": [
                    {
                        "id": self.category_id,
                        "snippet": {"assignable": self.assignable},
                    }
                ]
            }
        )


class _Videos:
    def __init__(self, verify_payload, *, prepare_error: Exception | None = None):
        self.verify_payload = verify_payload
        self.prepare_error = prepare_error
        self.insert_calls = []
        self.list_calls = []
        self.upload_request = _UploadRequest({"id": "video_123"})

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.upload_request

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _ExecuteRequest(self.verify_payload)


class _Service:
    def __init__(
        self,
        *,
        channel_id=CHANNEL_ID,
        long_uploads_status: str = "allowed",
        category_id: str = CATEGORY_ID,
        category_assignable: bool = True,
        verify_payload=None,
        prepare_error: Exception | None = None,
    ):
        self.channels_api = _Channels(
            channel_id,
            long_uploads_status=long_uploads_status,
        )
        self.video_categories_api = _VideoCategories(
            category_id=category_id,
            assignable=category_assignable,
        )
        self.videos_api = _Videos(
            _verified_payload() if verify_payload is None else verify_payload,
            prepare_error=prepare_error,
        )

    def channels(self):
        return self.channels_api

    def videoCategories(self):
        return self.video_categories_api

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
    now: datetime = NOW,
    category_id: str = CATEGORY_ID,
):
    token_path = tmp_path / "youtube-token.json"
    token_path.write_text("{}", encoding="utf-8")
    if os.name != "nt":
        os.chmod(token_path, 0o600)
    return YouTubePublishingProvider(
        YouTubePublishingConfig(
            token_path=str(token_path),
            channel_id=CHANNEL_ID,
            category_id=category_id,
            max_retries=4,
        ),
        credentials_loader=lambda path: object(),
        service_factory=lambda credentials: service,
        media_upload_factory=lambda path: {"path": str(path), "resumable": True},
        clock=lambda: now,
    )


def _preflight(provider, approved, media_path):
    assert provider.health().available is True
    provider.preflight(
        approved,
        media_path=media_path,
        idempotency_key=publish_idempotency_key(approved.request),
    )


def test_pr28_youtube_config_requires_absolute_token_path_and_decimal_category() -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        YouTubePublishingConfig(token_path="youtube-token.json", channel_id=CHANNEL_ID)
    with pytest.raises(ValueError, match="decimal category"):
        YouTubePublishingConfig(
            token_path="/local/youtube-token.json",
            channel_id=CHANNEL_ID,
            category_id="People & Blogs",
        )


def test_pr28_youtube_tag_budget_matches_space_and_comma_accounting() -> None:
    assert _youtube_tag_budget(("alpha", "two words")) == 5 + 1 + 9 + 2


def test_pr28_youtube_health_pins_exact_channel_and_provider_policy(tmp_path: Path) -> None:
    service = _Service()
    provider = _provider(tmp_path, service)
    assert provider.health() == PublishingProviderHealth(
        provider_id="youtube",
        provider_version="youtube_data_api_v3_pr28_v1:category=22:notify=0",
        available=True,
        reason=None,
    )
    assert service.channels_api.calls == [{"part": "id", "mine": True, "maxResults": 2}]

    mismatch = _provider(tmp_path, _Service(channel_id="UC_OTHER")).health()
    assert mismatch.available is False
    assert "configured destination" in (mismatch.reason or "")


def test_pr28_youtube_preflight_enforces_platform_metadata_before_remote_boundary(tmp_path: Path) -> None:
    artifact = _artifact()
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    provider = _provider(tmp_path, _Service())

    _preflight(provider, _approved(artifact, visibility="public"), media)

    invalid_cases = [
        (_approved(artifact, title="x" * 101), "at most 100"),
        (_approved(artifact, description="Ж" * 2501), "5000 UTF-8 bytes"),
        (
            _approved(
                artifact,
                visibility="private",
                scheduled_for=NOW + timedelta(hours=1),
            ),
            "requires approved public visibility",
        ),
        (
            _approved(
                artifact,
                visibility="public",
                scheduled_for=NOW - timedelta(seconds=1),
            ),
            "must be in the future",
        ),
    ]
    for approved, message in invalid_cases:
        assert provider.health().available is True
        with pytest.raises(PublishingPreflightError, match=message):
            provider.preflight(
                approved,
                media_path=media,
                idempotency_key=publish_idempotency_key(approved.request),
            )


def test_pr28_youtube_preflight_enforces_duration_and_long_upload_capability(tmp_path: Path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")

    too_long = _approved(_artifact(duration_seconds=12 * 60 * 60 + 0.1))
    provider = _provider(tmp_path, _Service())
    assert provider.health().available is True
    with pytest.raises(PublishingPreflightError, match="12-hour"):
        provider.preflight(
            too_long,
            media_path=media,
            idempotency_key=publish_idempotency_key(too_long.request),
        )

    long_artifact = _artifact(duration_seconds=15 * 60 + 1)
    eligible_service = _Service(long_uploads_status="eligible")
    eligible_provider = _provider(tmp_path, eligible_service)
    eligible = _approved(long_artifact)
    assert eligible_provider.health().available is True
    with pytest.raises(PublishingPreflightError, match="not currently allowed"):
        eligible_provider.preflight(
            eligible,
            media_path=media,
            idempotency_key=publish_idempotency_key(eligible.request),
        )
    assert eligible_service.channels_api.calls[-1] == {
        "part": "status",
        "mine": True,
        "maxResults": 2,
    }
    assert eligible_service.videos_api.insert_calls == []

    allowed_service = _Service(long_uploads_status="allowed")
    allowed_provider = _provider(tmp_path, allowed_service)
    allowed = _approved(long_artifact)
    _preflight(allowed_provider, allowed, media)
    assert allowed_service.channels_api.calls[-1] == {
        "part": "status",
        "mine": True,
        "maxResults": 2,
    }
    assert len(allowed_service.videos_api.insert_calls) == 1


def test_pr28_youtube_preflight_requires_assignable_category(tmp_path: Path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    service = _Service(category_assignable=False)
    provider = _provider(tmp_path, service)
    approved = _approved(_artifact())

    assert provider.health().available is True
    with pytest.raises(PublishingPreflightError, match="not assignable"):
        provider.preflight(
            approved,
            media_path=media,
            idempotency_key=publish_idempotency_key(approved.request),
        )
    assert service.video_categories_api.calls == [
        {"part": "snippet", "id": CATEGORY_ID}
    ]
    assert service.videos_api.insert_calls == []


def test_pr28_youtube_preflight_builds_request_before_remote_boundary(tmp_path: Path) -> None:
    artifact = _artifact()
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    service = _Service()
    provider = _provider(tmp_path, service)
    approved = _approved(artifact)

    _preflight(provider, approved, media)
    assert len(service.videos_api.insert_calls) == 1
    insert = service.videos_api.insert_calls[0]
    assert insert["part"] == "snippet,status"
    assert insert["notifySubscribers"] is False
    assert insert["body"]["snippet"]["categoryId"] == CATEGORY_ID
    assert service.videos_api.upload_request.calls == 0


def test_pr28_youtube_unscheduled_resumable_upload_and_verification(tmp_path: Path) -> None:
    artifact = _artifact()
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    service = _Service(verify_payload=_verified_payload(privacy="unlisted"))
    provider = _provider(tmp_path, service)
    approved = _approved(artifact, visibility="unlisted")

    _preflight(provider, approved, media)
    result = provider.publish(
        approved,
        media_path=media,
        idempotency_key=publish_idempotency_key(approved.request),
    )

    insert = service.videos_api.insert_calls[0]
    assert insert["body"] == {
        "snippet": {
            "title": "Approved upload",
            "description": "description",
            "categoryId": CATEGORY_ID,
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
    assert result.remote_url == "https://youtu.be/video_123"
    assert result.effective_at == datetime(2026, 9, 2, 7, 5, tzinfo=timezone.utc)
    assert result.evidence.provider_version.endswith(":category=22:notify=0")


def test_pr28_youtube_schedule_maps_public_approval_to_private_publish_at(tmp_path: Path) -> None:
    artifact = _artifact()
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    scheduled_for = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
    service = _Service(
        verify_payload=_verified_payload(
            privacy="private",
            publish_at="2026-09-02T08:00:00Z",
        )
    )
    provider = _provider(tmp_path, service)
    approved = _approved(
        artifact,
        visibility="public",
        scheduled_for=scheduled_for,
    )

    _preflight(provider, approved, media)
    result = provider.publish(
        approved,
        media_path=media,
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
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    service = _Service(
        verify_payload=_verified_payload(channel_id="UC_WRONG")
    )
    provider = _provider(tmp_path, service)
    approved = _approved(artifact)
    _preflight(provider, approved, media)
    with pytest.raises(PublishingResponseError, match="different channel"):
        provider.publish(
            approved,
            media_path=media,
            idempotency_key=publish_idempotency_key(approved.request),
        )


def test_pr28_youtube_verification_binds_approved_metadata_and_category(tmp_path: Path) -> None:
    artifact = _artifact()
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    service = _Service(
        verify_payload=_verified_payload(title="MUTATED REMOTE TITLE")
    )
    provider = _provider(tmp_path, service)
    approved = _approved(artifact)
    _preflight(provider, approved, media)
    with pytest.raises(PublishingResponseError, match="title does not match"):
        provider.publish(
            approved,
            media_path=media,
            idempotency_key=publish_idempotency_key(approved.request),
        )


def test_pr28_service_preflight_rejection_remains_retryable_failed(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path)
    artifact = _artifact()
    media = library.paths.root / artifact.output_storage_key
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"test")
    approved = _approved(artifact, title="x" * 101)
    service = PublishingService(
        library,
        _provider(tmp_path, _Service()),
        render_orchestrator=_ArtifactLoader(artifact),
    )

    attempt = service.prepare(approved)
    with pytest.raises(PublishAttemptError, match="preflight rejected"):
        service.execute_prepared(attempt.attempt_id)
    stored = library.publishing.get_attempt(attempt.attempt_id)
    assert stored is not None
    assert stored.state == "failed"
    assert stored.error_code == "provider_preflight_failed"
    assert stored.provider_health is None


def test_pr28_local_upload_request_construction_failure_stays_failed(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path)
    artifact = _artifact()
    media = library.paths.root / artifact.output_storage_key
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"test")
    approved = _approved(artifact)
    provider = _provider(
        tmp_path,
        _Service(prepare_error=RuntimeError("LOCAL_PREP_SECRET")),
    )
    service = PublishingService(
        library,
        provider,
        render_orchestrator=_ArtifactLoader(artifact),
    )

    attempt = service.prepare(approved)
    with pytest.raises(PublishAttemptError, match="preflight rejected") as caught:
        service.execute_prepared(attempt.attempt_id)
    assert "LOCAL_PREP_SECRET" not in str(caught.value)
    stored = library.publishing.get_attempt(attempt.attempt_id)
    assert stored is not None
    assert stored.state == "failed"
    assert stored.error_code == "provider_preflight_failed"
    assert "LOCAL_PREP_SECRET" not in stored.model_dump_json()
    assert not hasattr(provider._thread_state, "prepared")


def test_pr28_private_token_writer_is_atomic_and_owner_only(tmp_path: Path) -> None:
    token = tmp_path / "credentials" / "youtube-token.json"
    write_private_token(token, json.dumps({"refresh_token": "TOP_SECRET"}))
    assert json.loads(token.read_text(encoding="utf-8"))["refresh_token"] == "TOP_SECRET"
    assert not list(token.parent.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(token.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is privilege-sensitive on Windows")
def test_pr28_private_token_writer_rejects_final_component_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-token.json"
    real.write_text("ORIGINAL", encoding="utf-8")
    link = tmp_path / "youtube-token.json"
    link.symlink_to(real)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        write_private_token(link, json.dumps({"refresh_token": "TOP_SECRET"}))
    assert real.read_text(encoding="utf-8") == "ORIGINAL"
