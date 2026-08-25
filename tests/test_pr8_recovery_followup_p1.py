from __future__ import annotations

import os
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest

import content_forge.application.inbox as inbox_module
import content_forge.storage.asset_store as asset_store_module
import content_forge.storage.paths as paths_module
from content_forge.application import ApplicationRepository, InboxService, IntakeState
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


def test_full_staging_directory_chain_is_synced_before_full_acceptance(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_audio_probe(monkeypatch)

    chain_synced = False
    original_transition = service.repository.transition_intake

    def mark_chain_sync(path, *, stop_at):
        nonlocal chain_synced
        assert Path(path).resolve() == service.library.paths.incoming.resolve()
        assert Path(stop_at).resolve() == service.library.paths.root.resolve()
        chain_synced = True

    def checked_transition(
        intake_id,
        *,
        expected_state,
        update,
        durable=False,
    ):
        if update.get("content_sha256") is not None:
            assert chain_synced, "FULL acceptance preceded staging directory-chain fsync"
            assert durable is True
        return original_transition(
            intake_id,
            expected_state=expected_state,
            update=update,
            durable=durable,
        )

    monkeypatch.setattr(inbox_module, "fsync_directory_chain", mark_chain_sync)
    monkeypatch.setattr(service.repository, "transition_intake", checked_transition)

    intake = service.ingest_upload(BytesIO(b"full-staging-chain"), filename="chain.mp3")

    assert chain_synced
    assert intake.state is IntakeState.PREPARED


def test_project_linked_canonical_repair_eio_preserves_receipt_and_staging(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_audio_probe(monkeypatch)
    payload = b"linked-project-repair-must-remain-retryable"

    prepared = service.ingest_upload(BytesIO(payload), filename="linked.mp3")
    assert prepared.state is IntakeState.PREPARED
    assert prepared.asset_id is not None
    assert prepared.project_id is not None

    asset = service.library.database.get_asset(prepared.asset_id)
    assert asset is not None
    canonical = service.library.assets.resolve(asset)
    canonical.unlink()

    staged = service.library.paths.incoming / f"http-{prepared.intake_id}-recovery.mp3"
    staged.write_bytes(payload)

    receiving = service.repository.transition_intake(
        prepared.intake_id,
        expected_state=IntakeState.PREPARED,
        update={"state": IntakeState.RECEIVING},
    )
    assert receiving.asset_id == prepared.asset_id
    assert receiving.project_id == prepared.project_id
    assert service._verified_staging_candidate(receiving) == staged

    original_sync = asset_store_module.fsync_directory_chain

    def fail_sync(path, *, stop_at):
        assert Path(path).resolve() == canonical.parent.resolve()
        assert Path(stop_at).resolve() == service.library.paths.root.resolve()
        raise OSError("simulated canonical repair fsync EIO")

    monkeypatch.setattr(asset_store_module, "fsync_directory_chain", fail_sync)

    retry = service.reconcile_receiving()

    assert len(retry) == 1
    assert retry[0].state is IntakeState.RECEIVING
    assert retry[0].asset_id == prepared.asset_id
    assert retry[0].project_id == prepared.project_id
    assert service._verified_staging_candidate(retry[0]) == staged
    assert staged.read_bytes() == payload

    monkeypatch.setattr(asset_store_module, "fsync_directory_chain", original_sync)
    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].asset_id == prepared.asset_id
    assert recovered[0].project_id == prepared.project_id
    assert service._staging_candidates(recovered[0]) == ()
    repaired = service.library.database.get_asset(prepared.asset_id)
    assert repaired is not None
    assert service.library.assets.verify(repaired)


def test_live_upload_shutdown_after_full_acceptance_preserves_receipt_and_staging(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_audio_probe(monkeypatch)
    payload = b"accepted-live-upload-must-survive-shutdown"
    original_ingest = service.library.assets.ingest_file

    def interrupt_ingest(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(service.library.assets, "ingest_file", interrupt_ingest)

    with pytest.raises(KeyboardInterrupt):
        service.ingest_upload(BytesIO(payload), filename="shutdown.mp3")

    items = service.list_intakes()
    assert len(items) == 1
    interrupted = items[0]
    assert interrupted.state is IntakeState.RECEIVING
    assert interrupted.content_sha256 is not None
    assert interrupted.size_bytes == len(payload)
    assert interrupted.asset_id is None
    staged = service._verified_staging_candidate(interrupted)
    assert staged is not None
    assert staged.read_bytes() == payload

    monkeypatch.setattr(service.library.assets, "ingest_file", original_ingest)
    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].asset_id is not None
    assert service._staging_candidates(recovered[0]) == ()


def test_url_note_shutdown_signal_remains_recoverable(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    original_ensure_project = service._ensure_project

    def interrupt_project(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "_ensure_project", interrupt_project)

    with pytest.raises(KeyboardInterrupt):
        service.capture_url_note(
            source_url="https://example.invalid/shutdown",
            note="recover after shutdown",
        )

    items = service.list_intakes()
    assert len(items) == 1
    assert items[0].state is IntakeState.RECEIVING
    assert items[0].project_id is None

    monkeypatch.setattr(service, "_ensure_project", original_ensure_project)
    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].project_id is not None


def test_live_upload_operational_failure_after_project_linkage_returns_same_retryable_intake(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_audio_probe(monkeypatch)
    payload = b"accepted-linked-upload-must-retry-operational-failure"
    original_prepare = service._prepare_receiving_file
    calls = 0

    def fail_once(intake, asset):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert intake.asset_id is not None
            assert intake.project_id is not None
            raise sqlite3.OperationalError("simulated transient database busy")
        return original_prepare(intake, asset)

    monkeypatch.setattr(service, "_prepare_receiving_file", fail_once)

    retryable = service.ingest_upload(BytesIO(payload), filename="linked-retry.mp3")

    assert retryable.state is IntakeState.RECEIVING
    assert retryable.asset_id is not None
    assert retryable.project_id is not None
    assert retryable.error_code == "post_acceptance_retryable"
    items = service.list_intakes()
    assert len(items) == 1
    assert items[0].intake_id == retryable.intake_id
    assert items[0].asset_id == retryable.asset_id
    assert items[0].project_id == retryable.project_id
    # AssetStore is already authoritative at this checkpoint, so obsolete staging need
    # not survive merely to make the receipt resumable.
    assert service._staging_candidates(retryable) == ()

    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].intake_id == retryable.intake_id
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].asset_id == retryable.asset_id
    assert recovered[0].project_id == retryable.project_id
    assert len(service.list_intakes()) == 1


def test_live_upload_retains_staging_when_failure_receipt_lookup_also_fails(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    _install_audio_probe(monkeypatch)
    payload = b"accepted-staging-must-survive-secondary-receipt-read-failure"
    original_ingest = service.library.assets.ingest_file
    original_get_intake = service.repository.get_intake

    def fail_asset_publish(*args, **kwargs):
        raise OSError("simulated AssetStore ENOSPC")

    def fail_receipt_lookup(*args, **kwargs):
        raise sqlite3.OperationalError("simulated SQLite pressure during failure lookup")

    monkeypatch.setattr(service.library.assets, "ingest_file", fail_asset_publish)
    monkeypatch.setattr(service.repository, "get_intake", fail_receipt_lookup)

    with pytest.raises(OSError, match="AssetStore ENOSPC"):
        service.ingest_upload(BytesIO(payload), filename="lookup-failure.mp3")

    # Use the original bound method to inspect the durable receipt while the simulated
    # repository read failure remains installed on the service object.
    interrupted = next(iter(service.repository.list_intakes_in_state(IntakeState.RECEIVING)))
    assert original_get_intake(interrupted.intake_id) == interrupted
    assert interrupted.content_sha256 is not None
    assert interrupted.size_bytes == len(payload)
    assert interrupted.asset_id is None
    staged = service._verified_staging_candidate(interrupted)
    assert staged is not None
    assert staged.read_bytes() == payload

    monkeypatch.setattr(service.repository, "get_intake", original_get_intake)
    monkeypatch.setattr(service.library.assets, "ingest_file", original_ingest)
    recovered = service.reconcile_receiving()

    assert len(recovered) == 1
    assert recovered[0].state is IntakeState.PREPARED
    assert recovered[0].asset_id is not None
    assert service._staging_candidates(recovered[0]) == ()


def test_fresh_runtime_root_is_synced_through_preexisting_parent(
    tmp_path, monkeypatch
) -> None:
    if os.name == "nt":
        pytest.skip("portable directory fsync is unavailable on Windows")

    root = tmp_path / "new-parent" / "runtime"
    calls: list[tuple[Path, Path]] = []

    def record_chain(path, *, stop_at):
        calls.append((Path(path).resolve(), Path(stop_at).resolve()))

    monkeypatch.setattr(paths_module, "fsync_directory_chain", record_chain)

    paths = paths_module.RuntimePaths.from_root(root).ensure()

    assert paths.root == root
    assert root.is_dir()
    assert calls == [(root.resolve(), tmp_path.resolve())]
