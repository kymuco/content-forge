from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

from content_forge.providers import (
    PublishArtifactRef,
    PublishDeclarations,
    PublishMetadata,
    PublishRequest,
    PublishTarget,
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
)

CHANNEL_ID = "UC1234567890123456789012"


class _Videos:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.request = object()

    def insert(self, **kwargs):
        self.calls.append(kwargs)
        return self.request


class _Service:
    def __init__(self) -> None:
        self.videos_api = _Videos()

    def videos(self):
        return self.videos_api


def _approved():
    request = PublishRequest(
        contract_version="pr29_publish_contract_v2",
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
        metadata=PublishMetadata(title="Approved upload", visibility="private"),
        declarations=PublishDeclarations(
            child_directed=True,
            contains_realistic_altered_or_synthetic_media=False,
        ),
    )
    return approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 2, 9, 50, tzinfo=timezone.utc),
    )


def test_pr29_v2_preflight_replaces_local_insert_request_with_exact_declarations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    approved = _approved()
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"test")
    service = _Service()
    snapshot = io.BytesIO(b"test")

    provider = YouTubePublishingProvider(
        YouTubePublishingConfig(
            token_path=str((tmp_path / "token.json").resolve()),
            channel_id=CHANNEL_ID,
        ),
        media_upload_factory=lambda handle: {"handle": handle},
    )

    def fake_pr28_preflight(self, request, *, media_path, idempotency_key):
        self._thread_state.prepared = _PreparedUpload(
            service=service,
            insert_request=object(),
            request_sha256=semantic_publish_request_digest(request.request),
            idempotency_key=idempotency_key,
            media_path=media_path,
            media_snapshot=snapshot,
        )

    monkeypatch.setattr(PR28YouTubePublishingProvider, "preflight", fake_pr28_preflight)

    key = publish_idempotency_key(approved.request)
    provider.preflight(approved, media_path=media_path, idempotency_key=key)

    assert len(service.videos_api.calls) == 1
    call = service.videos_api.calls[0]
    assert call["part"] == "snippet,status"
    assert call["notifySubscribers"] is False
    body = call["body"]
    assert isinstance(body, dict)
    status = body["status"]
    assert isinstance(status, dict)
    assert status["privacyStatus"] == "private"
    assert status["selfDeclaredMadeForKids"] is True
    assert status["containsSyntheticMedia"] is False

    prepared = provider._thread_state.prepared
    assert isinstance(prepared, _PreparedUpload)
    assert prepared.insert_request is service.videos_api.request
    assert prepared.media_snapshot is snapshot
