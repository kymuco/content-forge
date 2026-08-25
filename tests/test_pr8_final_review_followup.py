from __future__ import annotations

import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import content_forge.application.inbox as inbox_module
import content_forge.application.media as media_module
from content_forge.api import create_app
from content_forge.api.app import PARSED_BODY_LIMIT
from content_forge.application import ApplicationRepository, InboxService, IntakeState
from content_forge.render.ffmpeg import MediaProbe
from content_forge.storage import LocalLibrary


def test_pairing_exchange_and_authenticated_json_are_bounded_before_parsing(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        oversized = client.post(
            "/api/v1/pairing/exchange",
            content=b"x" * (PARSED_BODY_LIMIT + 1),
            headers={"Host": "localhost", "Content-Type": "text/plain"},
        )
        assert oversized.status_code == 413

        missing_length_request = client.build_request(
            "POST",
            "/api/v1/pairing/exchange",
            content=b"{}",
            headers={"Host": "localhost", "Content-Type": "application/json"},
        )
        del missing_length_request.headers["content-length"]
        missing_length = client.send(missing_length_request)
        assert missing_length.status_code == 411

        # URL/note is authenticated before FastAPI gets a chance to parse malformed JSON.
        unauthenticated = client.post(
            "/api/v1/inbox/url-note",
            content=b"not-json",
            headers={"Host": "localhost", "Content-Type": "application/json"},
        )
        assert unauthenticated.status_code == 401
        assert app.state.inbox.list_intakes() == ()
    finally:
        app.state.runtime_lease.close()


def test_thumbnail_directory_sync_oserror_keeps_intake_retryable(tmp_path, monkeypatch) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    service = InboxService(library, repository, max_upload_bytes=1024)
    payload = b"visual-upload-with-retryable-thumbnail-storage-failure"

    monkeypatch.setattr(
        inbox_module,
        "probe_media",
        lambda path, ffprobe_path="ffprobe": MediaProbe(
            path=str(path),
            format_name="mp4",
            duration_seconds=1.0,
            has_video=True,
            has_audio=False,
            video_codec="h264",
            width=320,
            height=240,
            fps=30.0,
        ),
    )

    def fake_run(arguments, **kwargs):
        Path(arguments[-1]).write_bytes(b"synthetic-jpeg")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)
    original_sync = media_module.fsync_directory_chain
    sync_calls = 0

    def fail_once(path, *, stop_at):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise OSError("simulated thumbnail directory fsync EIO")
        return original_sync(path, stop_at=stop_at)

    monkeypatch.setattr(media_module, "fsync_directory_chain", fail_once)

    with pytest.raises(OSError, match="thumbnail directory fsync EIO"):
        service.ingest_upload(BytesIO(payload), filename="visual.mp4")

    items = service.list_intakes()
    assert len(items) == 1
    retryable = items[0]
    assert retryable.state is IntakeState.RECEIVING
    assert retryable.asset_id is not None
    assert retryable.project_id is not None
    assert retryable.error_code == "post_acceptance_retryable"

    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].asset_id == retryable.asset_id
    assert recovered[0].project_id == retryable.project_id
    thumbnail = service.thumbnail_path(recovered[0].asset_id)
    assert thumbnail is not None
    assert thumbnail.read_bytes() == b"synthetic-jpeg"
