from __future__ import annotations

import hashlib
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
from content_forge.core import EntityKind, MediaType, Project, ProjectState, new_entity_id
from content_forge.render.ffmpeg import MediaProbe, MediaProbeError
from content_forge.storage import DerivativeSlot, LocalLibrary


def _service(tmp_path) -> InboxService:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    return InboxService(library, repository, max_upload_bytes=1024)


def _install_video_preparation(monkeypatch) -> None:
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


def test_upload_classification_comes_from_probe_not_client_mime_or_filename(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_video_preparation(monkeypatch)
    payload = b"synthetic-video-bytes"

    intake = service.ingest_upload(
        BytesIO(payload),
        filename="misleading.txt",
        mime_type="text/plain",
        source_url="https://example.invalid/source",
        note="captured from phone",
        content_kind_hint="character_moment",
    )

    assert intake.state is IntakeState.PREPARED
    assert intake.probe_state is PreparationState.SUCCEEDED
    assert intake.thumbnail_state is PreparationState.SUCCEEDED
    assert intake.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert intake.asset_id is not None
    assert intake.source_id is not None
    assert intake.project_id is not None

    asset = service.library.database.get_asset(intake.asset_id)
    assert asset is not None
    assert asset.media_type.value == "video"
    assert asset.mime_type == "video/mp4"
    assert (asset.width, asset.height) == (32, 48)

    source = service.library.database.get_source(intake.source_id)
    assert source is not None
    assert source.asset_id == intake.asset_id
    assert source.source_url == "https://example.invalid/source"
    assert source.original_title == "misleading.txt"

    project = service.library.load_project(intake.project_id)
    assert project is not None
    assert project.state.value == "inbox"
    assert project.content_kind == "character_moment"
    assert project.source_refs[0].asset_id == intake.asset_id
    assert project.source_refs[0].source_id == intake.source_id


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
    assert intake.content_sha256 is not None
    assert intake.asset_id is not None
    assert intake.source_id is not None
    assert intake.project_id is not None
    assert intake.error_message == "operation failed"
    assert str(tmp_path) not in (intake.error_message or "")
    assert service.library.database.get_asset(intake.asset_id) is not None
    assert service.library.database.get_source(intake.source_id) is not None
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


def test_project_identity_converges_before_receipt_link(tmp_path) -> None:
    service = _service(tmp_path)
    intake = service.repository.create_intake(
        InboxIntake(
            kind=IntakeKind.URL_NOTE,
            source_url="https://example.invalid/concurrent-recovery",
            probe_state=PreparationState.SKIPPED,
            thumbnail_state=PreparationState.SKIPPED,
        )
    )

    # Model two reconcilers that both observed the same unlinked receipt before either
    # committed a Project. They must independently construct byte-identical manifests.
    project_a = service._build_url_project(intake)
    project_b = service._build_url_project(intake)
    assert project_a == project_b
    assert project_a.project_id == f"cf_project_{intake.intake_id.rsplit('_', 1)[1]}"
    assert project_a.created_at == intake.created_at
    assert project_a.updated_at == intake.created_at

    service.library.save_project(project_a)
    service.library.save_project(project_b)
    assert service.repository.find_project_for_intake(intake.intake_id) == project_a


def test_startup_reconciliation_recovers_asset_row_before_receipt_link(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_video_preparation(monkeypatch)
    payload = b"accepted-before-receipt-link"
    digest = hashlib.sha256(payload).hexdigest()
    source_id = new_entity_id(EntityKind.SOURCE)
    intake = service.repository.create_intake(
        InboxIntake(
            kind=IntakeKind.FILE,
            original_name="crash-window.mp4",
            mime_type="video/mp4",
            size_bytes=len(payload),
            content_sha256=digest,
            source_url="https://example.invalid/crash-window",
            note="recover provenance too",
            source_id=source_id,
        )
    )
    staged = tmp_path / "crash-window.mp4"
    staged.write_bytes(payload)
    result = service.library.assets.ingest_file(
        staged,
        source=None,
        media_type=MediaType.OTHER,
        mime_type="application/octet-stream",
    )
    assert intake.asset_id is None
    assert service.library.database.get_asset(result.asset.asset_id) is not None

    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    item = recovered[0]
    assert item.state is IntakeState.PREPARED
    assert item.asset_id == result.asset.asset_id
    assert item.source_id == source_id
    assert item.project_id is not None
    source = service.library.database.get_source(source_id)
    assert source is not None
    assert source.asset_id == result.asset.asset_id
    assert source.source_url == "https://example.invalid/crash-window"
    project = service.library.load_project(item.project_id)
    assert project is not None
    assert project.source_refs[0].source_id == source_id


def test_startup_reconciliation_recovers_blob_before_asset_row(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_video_preparation(monkeypatch)
    payload = b"published-blob-before-catalog-row"
    digest = hashlib.sha256(payload).hexdigest()
    source_id = new_entity_id(EntityKind.SOURCE)
    service.repository.create_intake(
        InboxIntake(
            kind=IntakeKind.FILE,
            original_name="blob-only.mp4",
            size_bytes=len(payload),
            content_sha256=digest,
            source_id=source_id,
        )
    )
    blob_path = service.library.paths.blob_path_for_sha256(digest)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(payload)
    assert service.library.database.get_asset_by_sha256(digest) is None

    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    item = recovered[0]
    assert item.state is IntakeState.PREPARED
    assert item.asset_id is not None
    assert item.source_id == source_id
    asset = service.library.database.get_asset(item.asset_id)
    assert asset is not None
    assert asset.sha256 == digest
    assert asset.size_bytes == len(payload)
    assert service.library.assets.verify(asset)
    source = service.library.database.get_source(source_id)
    assert source is not None
    assert source.asset_id == item.asset_id
    assert item.project_id is not None


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
