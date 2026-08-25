from __future__ import annotations

import sqlite3
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


def _install_visual_probe(monkeypatch) -> None:
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


def _install_audio_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        inbox_module,
        "probe_media",
        lambda path, ffprobe_path="ffprobe": MediaProbe(
            path=str(path),
            format_name="mp3",
            duration_seconds=1.0,
            has_video=False,
            has_audio=True,
            audio_codec="mp3",
        ),
    )


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

    _install_visual_probe(monkeypatch)

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


def test_thumbnail_ffmpeg_launch_oserror_is_partial_generation_failure(
    tmp_path, monkeypatch
) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    service = InboxService(library, repository, max_upload_bytes=1024)
    _install_visual_probe(monkeypatch)

    def fail_launch(*args, **kwargs):
        raise FileNotFoundError("configured ffmpeg binary is missing")

    monkeypatch.setattr(media_module.subprocess, "run", fail_launch)

    intake = service.ingest_upload(
        BytesIO(b"visual-with-permanent-ffmpeg-launch-failure"),
        filename="visual.mp4",
    )

    assert intake.state is IntakeState.PARTIAL
    assert intake.error_code == "thumbnail_failed"
    assert intake.error_message == "thumbnail generation failed"
    assert intake.asset_id is not None
    assert intake.project_id is not None
    persisted = service.get_intake(intake.intake_id)
    assert persisted is not None
    assert persisted.state is IntakeState.PARTIAL
    assert persisted.error_code == "thumbnail_failed"
    assert service.reconcile_receiving() == ()


def test_thumbnail_mapping_excludes_attached_picture_streams(tmp_path, monkeypatch) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    service = InboxService(library, repository, max_upload_bytes=1024)
    _install_visual_probe(monkeypatch)
    captured_arguments: tuple[str, ...] | None = None

    def fake_run(arguments, **kwargs):
        nonlocal captured_arguments
        captured_arguments = tuple(arguments)
        Path(arguments[-1]).write_bytes(b"thumbnail-from-real-video-stream")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)

    intake = service.ingest_upload(
        BytesIO(b"video-with-leading-attached-picture-stream"),
        filename="video-with-cover.mp4",
    )

    assert intake.state is IntakeState.PREPARED
    assert captured_arguments is not None
    map_index = captured_arguments.index("-map")
    assert captured_arguments[map_index + 1] == "0:V:0"
    assert "0:v:0" not in captured_arguments
    assert intake.asset_id is not None
    thumbnail = service.thumbnail_path(intake.asset_id)
    assert thumbnail is not None
    assert thumbnail.read_bytes() == b"thumbnail-from-real-video-stream"


def test_url_note_sqlite_operational_after_project_linkage_returns_same_recoverable_intake(
    tmp_path, monkeypatch
) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    service = InboxService(library, repository, max_upload_bytes=1024)
    original_transition = repository.transition_intake
    failed_once = False

    def fail_final_once(
        intake_id,
        *,
        expected_state,
        update,
        durable=False,
    ):
        nonlocal failed_once
        if not failed_once and update.get("state") is IntakeState.PREPARED:
            failed_once = True
            raise sqlite3.OperationalError("database is locked")
        return original_transition(
            intake_id,
            expected_state=expected_state,
            update=update,
            durable=durable,
        )

    monkeypatch.setattr(repository, "transition_intake", fail_final_once)

    retryable = service.capture_url_note(
        source_url="https://example.com/item",
        note="retry me",
    )

    assert retryable.state is IntakeState.RECEIVING
    assert retryable.project_id is not None
    assert retryable.error_code == "capture_retryable"
    items = service.list_intakes()
    assert len(items) == 1
    assert items[0].intake_id == retryable.intake_id
    assert items[0].project_id == retryable.project_id
    project_id = retryable.project_id

    monkeypatch.setattr(repository, "transition_intake", original_transition)
    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].intake_id == retryable.intake_id
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].project_id == project_id
    assert len(service.list_intakes()) == 1
    project = service.library.load_project(project_id)
    assert project is not None
    assert project.metadata["inbox_intake_id"] == recovered[0].intake_id


def test_upload_staging_uses_fixed_suffix_for_pathological_filename(
    tmp_path, monkeypatch
) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    service = InboxService(library, repository, max_upload_bytes=1024)
    _install_audio_probe(monkeypatch)
    filename = "a." + ("x" * 300)

    intake = service.ingest_upload(
        BytesIO(b"pathological-extension-is-provenance-only"),
        filename=filename,
    )

    assert intake.state is IntakeState.PREPARED
    assert intake.original_name == filename
    assert intake.asset_id is not None
    assert intake.project_id is not None
    project = service.library.load_project(intake.project_id)
    assert project is not None
    assert project.metadata["original_filename"] == filename
