from __future__ import annotations

import json
import sqlite3
import subprocess
from io import BytesIO
from pathlib import Path

import pytest

import content_forge.application.inbox as inbox_module
import content_forge.application.media as media_module
import content_forge.render.ffmpeg.probe as probe_module
import content_forge.storage.asset_store as asset_store_module
from content_forge.api import create_app
from content_forge.application import ApplicationRepository, InboxService, IntakeState
from content_forge.application.media import authoritative_media_classification, generate_thumbnail
from content_forge.application.runtime_lock import RuntimeBusyError
from content_forge.core import MediaType
from content_forge.render.ffmpeg import MediaProbe
from content_forge.storage import LocalLibrary


def _service(tmp_path) -> InboxService:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    return InboxService(library, repository, max_upload_bytes=1024)


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


def test_post_acceptance_assetstore_failure_preserves_verified_staging_for_recovery(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    payload = b"accepted-before-assetstore-enospc"
    original_ingest = service.library.assets.ingest_file

    def fail_ingest(*args, **kwargs):
        raise OSError("ENOSPC during AssetStore publication")

    monkeypatch.setattr(service.library.assets, "ingest_file", fail_ingest)
    with pytest.raises(OSError, match="ENOSPC"):
        service.ingest_upload(BytesIO(payload), filename="accepted.mp3")

    intake = service.list_intakes()[0]
    assert intake.state is IntakeState.RECEIVING
    assert intake.asset_id is None
    assert intake.content_sha256 is not None
    assert intake.size_bytes == len(payload)
    assert intake.error_code == "post_acceptance_retryable"
    staged = service._verified_staging_candidate(intake)
    assert staged is not None
    assert staged.read_bytes() == payload

    # A restart while storage pressure still exists must not convert accepted bytes into
    # terminal failure or delete the only authenticated copy.
    retry = service.reconcile_receiving()
    assert len(retry) == 1
    assert retry[0].state is IntakeState.RECEIVING
    assert service._verified_staging_candidate(retry[0]) == staged

    monkeypatch.setattr(service.library.assets, "ingest_file", original_ingest)
    _install_audio_probe(monkeypatch)
    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].asset_id is not None
    assert service._staging_candidates(recovered[0]) == ()


def test_sqlite_operational_storage_pressure_after_acceptance_remains_resumable(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    payload = b"accepted-before-sqlite-pressure"
    original_ingest = service.library.assets.ingest_file

    def initial_filesystem_pressure(*args, **kwargs):
        raise OSError("ENOSPC before canonical publication")

    monkeypatch.setattr(
        service.library.assets,
        "ingest_file",
        initial_filesystem_pressure,
    )
    with pytest.raises(OSError, match="ENOSPC"):
        service.ingest_upload(BytesIO(payload), filename="sqlite-retry.mp3")

    intake = service.list_intakes()[0]
    staged = service._verified_staging_candidate(intake)
    assert staged is not None
    assert staged.read_bytes() == payload

    def sqlite_pressure(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(service.library.assets, "ingest_file", sqlite_pressure)
    retry = service.reconcile_receiving()

    assert len(retry) == 1
    assert retry[0].state is IntakeState.RECEIVING
    assert retry[0].content_sha256 == intake.content_sha256
    assert retry[0].size_bytes == len(payload)
    assert service._verified_staging_candidate(retry[0]) == staged
    assert staged.read_bytes() == payload

    monkeypatch.setattr(service.library.assets, "ingest_file", original_ingest)
    _install_audio_probe(monkeypatch)
    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].content_sha256 == intake.content_sha256
    assert recovered[0].size_bytes == len(payload)
    assert service._staging_candidates(recovered[0]) == ()


def test_tampered_accepted_staging_fails_terminally_instead_of_retrying_forever(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)

    def fail_ingest(*args, **kwargs):
        raise OSError("ENOSPC during AssetStore publication")

    monkeypatch.setattr(service.library.assets, "ingest_file", fail_ingest)
    with pytest.raises(OSError, match="ENOSPC"):
        service.ingest_upload(BytesIO(b"frozen-good-bytes"), filename="accepted.mp3")

    intake = service.list_intakes()[0]
    staged = service._verified_staging_candidate(intake)
    assert staged is not None
    staged.write_bytes(b"tampered-bytes")

    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.FAILED
    assert recovered[0].error_code == "interrupted_recovery_failed"
    assert service._staging_candidates(recovered[0]) == ()


def test_staging_directory_is_synced_before_byte_acceptance_receipt(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_audio_probe(monkeypatch)
    directory_synced = False
    original_transition = service.repository.transition_intake

    def mark_directory_sync(path):
        nonlocal directory_synced
        assert path == service.library.paths.incoming
        directory_synced = True

    def checked_transition(intake_id, *, expected_state, update):
        if update.get("content_sha256") is not None:
            assert directory_synced, "byte receipt committed before incoming directory fsync"
        return original_transition(
            intake_id,
            expected_state=expected_state,
            update=update,
        )

    monkeypatch.setattr(inbox_module, "_fsync_directory", mark_directory_sync)
    monkeypatch.setattr(service.repository, "transition_intake", checked_transition)

    intake = service.ingest_upload(BytesIO(b"directory-durable"), filename="x.mp3")
    assert directory_synced
    assert intake.state is IntakeState.PREPARED


def test_assetstore_syncs_blob_directory_chain_before_first_catalog_receipt(
    tmp_path, monkeypatch
) -> None:
    library = LocalLibrary(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"canonical-blob-durability")
    directory_synced = False
    original_put_asset = library.database.put_asset

    def mark_directory_sync(path, *, stop_at):
        nonlocal directory_synced
        assert Path(stop_at).resolve() == library.paths.root.resolve()
        assert Path(path).is_dir()
        directory_synced = True

    def checked_put_asset(asset):
        assert directory_synced, "asset row committed before canonical blob directory fsync"
        canonical = library.paths.blob_path_for_sha256(asset.sha256)
        assert canonical.is_file()
        return original_put_asset(asset)

    monkeypatch.setattr(asset_store_module, "fsync_directory_chain", mark_directory_sync)
    monkeypatch.setattr(library.database, "put_asset", checked_put_asset)

    result = library.assets.ingest_file(
        source,
        source=None,
        media_type=MediaType.OTHER,
        mime_type="application/octet-stream",
    )

    assert directory_synced
    assert library.assets.verify(result.asset)


def test_thumbnail_directory_is_synced_before_derivative_receipt(
    tmp_path, monkeypatch
) -> None:
    library = LocalLibrary(tmp_path)
    source = tmp_path / "source.png"
    source.write_bytes(b"thumbnail-source")
    asset = library.assets.ingest_file(
        source,
        source=None,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    ).asset
    source_path = library.assets.resolve(asset)
    directory_synced = False
    original_put_slot = library.database.put_derivative_slot

    def fake_run(arguments, **kwargs):
        Path(arguments[-1]).write_bytes(b"synthetic-jpeg")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def mark_directory_sync(path, *, stop_at):
        nonlocal directory_synced
        assert Path(stop_at).resolve() == library.paths.root.resolve()
        directory = Path(path)
        assert directory.is_dir()
        assert any(directory.glob("*-v1-360x640.jpg")), "thumbnail rename must precede fsync"
        directory_synced = True

    def checked_put_slot(slot):
        assert directory_synced, "derivative receipt committed before thumbnail directory fsync"
        return original_put_slot(slot)

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)
    monkeypatch.setattr(media_module, "fsync_directory_chain", mark_directory_sync)
    monkeypatch.setattr(library.database, "put_derivative_slot", checked_put_slot)

    result = generate_thumbnail(library, asset, source_path)

    assert result is not None
    assert directory_synced
    assert result.path.is_file()
    slot = library.database.get_derivative_slot(asset.asset_id, "thumbnail.default")
    assert slot is not None
    assert slot.metadata["sha256"] == result.sha256


def test_api_runtime_root_has_one_live_owner(tmp_path) -> None:
    first = create_app(root=tmp_path)
    try:
        with pytest.raises(RuntimeBusyError, match="already owned"):
            create_app(root=tmp_path)
    finally:
        first.state.runtime_lease.close()

    second = create_app(root=tmp_path)
    second.state.runtime_lease.close()


def test_probe_ignores_attached_cover_art_as_video_stream(tmp_path, monkeypatch) -> None:
    source = tmp_path / "song.mp3"
    source.write_bytes(b"synthetic-audio")
    payload = {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "mp3",
                "duration": "3.0",
            },
            {
                "codec_type": "video",
                "codec_name": "mjpeg",
                "width": 600,
                "height": 600,
                "avg_frame_rate": "0/0",
                "r_frame_rate": "0/0",
                "disposition": {"attached_pic": 1},
            },
        ],
        "format": {"format_name": "mp3", "duration": "3.0"},
    }

    monkeypatch.setattr(
        probe_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    probe = probe_module.probe_media(source)
    assert probe.has_audio is True
    assert probe.has_video is False
    assert probe.video_codec is None
    assert probe.width is None
    assert probe.height is None
    media_type, mime_type = authoritative_media_classification(probe)
    assert media_type is MediaType.AUDIO
    assert mime_type == "audio/mpeg"
