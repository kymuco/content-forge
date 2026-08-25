from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

import content_forge.application.inbox as inbox_module
import content_forge.storage.asset_store as asset_store_module
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
