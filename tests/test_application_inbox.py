from __future__ import annotations

from io import BytesIO

import pytest

import content_forge.application.inbox as inbox_module
from content_forge.application import (
    ApplicationRepository,
    InboxService,
    IntakeState,
    PreparationState,
    UploadTooLargeError,
)
from content_forge.render.ffmpeg import MediaProbe, MediaProbeError
from content_forge.storage import LocalLibrary


def _service(tmp_path) -> InboxService:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    return InboxService(library, repository, max_upload_bytes=1024)


def test_upload_ingest_uses_filename_mime_fallback_and_creates_inbox_project(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        inbox_module,
        "probe_media",
        lambda path, ffprobe_path="ffprobe": MediaProbe(
            path=str(path),
            format_name="png_pipe",
            width=32,
            height=48,
            has_video=True,
            has_audio=False,
            video_codec="png",
        ),
    )
    monkeypatch.setattr(inbox_module, "generate_thumbnail", lambda *args, **kwargs: object())

    intake = service.ingest_upload(
        BytesIO(b"synthetic-image-bytes"),
        filename="frame.png",
        mime_type="application/octet-stream",
        source_url="https://example.invalid/source",
        note="captured from phone",
        content_kind_hint="character_moment",
    )

    assert intake.state is IntakeState.PREPARED
    assert intake.probe_state is PreparationState.SUCCEEDED
    assert intake.thumbnail_state is PreparationState.SUCCEEDED
    assert intake.asset_id is not None
    assert intake.project_id is not None

    asset = service.library.database.get_asset(intake.asset_id)
    assert asset is not None
    assert asset.mime_type == "image/png"
    assert (asset.width, asset.height) == (32, 48)

    project = service.library.load_project(intake.project_id)
    assert project is not None
    assert project.state.value == "inbox"
    assert project.content_kind == "character_moment"
    assert project.source_refs[0].asset_id == intake.asset_id


def test_probe_failure_retains_asset_and_project_as_partial(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)

    def fail_probe(*args, **kwargs):
        raise MediaProbeError("synthetic probe failure")

    monkeypatch.setattr(inbox_module, "probe_media", fail_probe)

    intake = service.ingest_upload(
        BytesIO(b"still-retained"),
        filename="broken.mp4",
        mime_type="video/mp4",
    )

    assert intake.state is IntakeState.PARTIAL
    assert intake.probe_state is PreparationState.FAILED
    assert intake.thumbnail_state is PreparationState.SKIPPED
    assert intake.error_code == "media_probe_failed"
    assert service.library.database.get_asset(intake.asset_id) is not None
    assert service.library.load_project(intake.project_id) is not None


def test_oversized_upload_is_durably_failed(tmp_path) -> None:
    service = _service(tmp_path)
    service.max_upload_bytes = 4

    with pytest.raises(UploadTooLargeError):
        service.ingest_upload(BytesIO(b"12345"), filename="big.bin")

    items = service.list_intakes()
    assert len(items) == 1
    assert items[0].state is IntakeState.FAILED
    assert items[0].error_code == "UploadTooLargeError"


def test_url_note_capture_creates_project_without_fake_asset(tmp_path) -> None:
    service = _service(tmp_path)
    intake = service.capture_url_note(
        source_url="https://example.invalid/post/1",
        note="download later",
        creator_hint="Example Creator",
    )

    assert intake.state is IntakeState.PREPARED
    assert intake.asset_id is None
    assert intake.project_id is not None
    project = service.library.load_project(intake.project_id)
    assert project is not None
    assert project.source_refs == ()
    assert project.metadata["source_url"] == "https://example.invalid/post/1"
