from __future__ import annotations

from io import BytesIO

import pytest

import content_forge.application.inbox as inbox_module
from content_forge.application import (
    ApplicationRepository,
    InboxError,
    InboxService,
    IntakeKind,
    IntakeState,
    PreparationState,
    UploadTooLargeError,
)
from content_forge.application.models import InboxIntake
from content_forge.core import Project, ProjectState
from content_forge.render.ffmpeg import MediaProbe, MediaProbeError
from content_forge.storage import DerivativeSlot, LocalLibrary


def _service(tmp_path) -> InboxService:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    return InboxService(library, repository, max_upload_bytes=1024)


def test_upload_classification_comes_from_probe_not_client_mime_or_filename(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        inbox_module,
        "probe_media",
        lambda path, ffprobe_path="ffprobe": MediaProbe(
            path=str(path),
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=1.0,
            width=32,
            height=48,
            fps=24.0,
            has_video=True,
            has_audio=False,
            video_codec="h264",
        ),
    )
    monkeypatch.setattr(inbox_module, "generate_thumbnail", lambda *args, **kwargs: object())

    intake = service.ingest_upload(
        BytesIO(b"synthetic-video-bytes"),
        filename="misleading.txt",
        mime_type="text/plain",
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
    assert asset.media_type.value == "video"
    assert asset.mime_type == "video/mp4"
    assert (asset.width, asset.height) == (32, 48)

    project = service.library.load_project(intake.project_id)
    assert project is not None
    assert project.state.value == "inbox"
    assert project.content_kind == "character_moment"
    assert project.source_refs[0].asset_id == intake.asset_id


def test_probe_failure_retains_asset_and_project_as_partial_without_path_leak(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)

    def fail_probe(path, **kwargs):
        raise MediaProbeError(f"failed to read {path}")

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
    assert intake.error_message == "media probe failed"
    assert str(tmp_path) not in (intake.error_message or "")
    assert service.library.database.get_asset(intake.asset_id) is not None
    assert service.library.load_project(intake.project_id) is not None


def test_unexpected_post_ingest_failure_keeps_asset_and_project_linkage(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError(f"unexpected failure at {tmp_path}")

    monkeypatch.setattr(inbox_module, "probe_media", explode)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        service.ingest_upload(
            BytesIO(b"retained-after-unexpected-failure"),
            filename="unexpected.mp4",
            mime_type="video/mp4",
        )

    intake = service.list_intakes()[0]
    assert intake.state is IntakeState.FAILED
    assert intake.asset_id is not None
    assert intake.project_id is not None
    assert intake.error_message == "operation failed"
    assert str(tmp_path) not in (intake.error_message or "")
    assert service.library.database.get_asset(intake.asset_id) is not None
    assert service.library.load_project(intake.project_id) is not None


def test_startup_reconciliation_recovers_project_to_receipt_crash_window(tmp_path) -> None:
    service = _service(tmp_path)
    intake = service.repository.create_intake(
        InboxIntake(
            kind=IntakeKind.URL_NOTE,
            source_url="https://example.invalid/recover",
            note="recover me",
            probe_state=PreparationState.SKIPPED,
            thumbnail_state=PreparationState.SKIPPED,
        )
    )
    project = Project(
        content_kind="unclassified",
        state=ProjectState.INBOX,
        metadata={
            "inbox_intake_id": intake.intake_id,
            "source_url": intake.source_url,
            "note": intake.note,
        },
    )
    service.library.save_project(project)

    recovered = service.reconcile_receiving()
    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].project_id == project.project_id
    assert service.repository.find_project_for_intake(intake.intake_id) == project


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


def test_thumbnail_endpoint_resolution_rejects_noncanonical_runtime_key(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        inbox_module,
        "probe_media",
        lambda path, ffprobe_path="ffprobe": MediaProbe(
            path=str(path),
            format_name="png_pipe",
            width=4,
            height=4,
            has_video=True,
            has_audio=False,
            video_codec="png",
        ),
    )
    monkeypatch.setattr(inbox_module, "generate_thumbnail", lambda *args, **kwargs: object())
    intake = service.ingest_upload(BytesIO(b"x"), filename="x.png")
    assert intake.asset_id is not None
    asset = service.library.database.get_asset(intake.asset_id)
    assert asset is not None

    service.library.database.put_derivative_slot(
        DerivativeSlot(
            asset_id=asset.asset_id,
            slot="thumbnail.default",
            storage_key="content-forge.sqlite3",
            metadata={
                "source_sha256": asset.sha256,
                "sha256": "0" * 64,
            },
        )
    )
    with pytest.raises(InboxError, match="not canonical"):
        service.thumbnail_path(asset.asset_id)
