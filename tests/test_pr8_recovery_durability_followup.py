from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import content_forge.storage.asset_store as asset_store_module
from content_forge.core import MediaType
from content_forge.storage import LocalLibrary


def test_existing_canonical_blob_is_resynced_before_first_catalog_receipt(
    tmp_path, monkeypatch
) -> None:
    """A pathname surviving a failed prior dir-fsync cannot outrun its first asset row."""

    library = LocalLibrary(tmp_path)
    payload = b"rename-survived-but-directory-sync-did-not"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)

    digest = hashlib.sha256(payload).hexdigest()
    canonical = library.paths.blob_path_for_sha256(digest)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(payload)
    assert library.database.get_asset_by_sha256(digest) is None

    directory_synced = False
    original_put_asset = library.database.put_asset

    def mark_directory_sync(path, *, stop_at):
        nonlocal directory_synced
        assert Path(path).resolve() == canonical.parent.resolve()
        assert Path(stop_at).resolve() == library.paths.root.resolve()
        directory_synced = True

    def checked_put_asset(asset):
        assert directory_synced, "catalog row committed before recovered canonical dir fsync"
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
    assert result.asset.sha256 == digest
    assert library.database.get_asset_by_sha256(digest) is not None


def test_verified_canonical_blob_must_reestablish_directory_durability(
    tmp_path, monkeypatch
) -> None:
    """Recovery byte verification must not silently accept a failed durability barrier."""

    library = LocalLibrary(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"catalog-row-plus-canonical-recovery")
    asset = library.assets.ingest_file(
        source,
        source=None,
        media_type=MediaType.OTHER,
        mime_type="application/octet-stream",
    ).asset

    def fail_directory_sync(path, *, stop_at):
        assert Path(path).resolve() == library.assets.resolve(asset).parent.resolve()
        assert Path(stop_at).resolve() == library.paths.root.resolve()
        raise OSError("simulated directory fsync EIO")

    monkeypatch.setattr(asset_store_module, "fsync_directory_chain", fail_directory_sync)

    with pytest.raises(OSError, match="fsync EIO"):
        library.assets.verify(asset)

    # Byte integrity is still intact; only the durability barrier is intentionally
    # unresolved, so a later retry can re-run verification/sync rather than lose data.
    assert library.assets.resolve(asset).read_bytes() == b"catalog-row-plus-canonical-recovery"
