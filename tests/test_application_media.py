from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import content_forge.application.media as media_module
from content_forge.application import ApplicationRepository
from content_forge.application.media import generate_thumbnail
from content_forge.core import MediaType
from content_forge.storage import LocalLibrary


def test_authoritative_enrichment_repairs_legacy_classification(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    source = tmp_path / "legacy-misleading.txt"
    source.write_bytes(b"legacy-bytes")
    legacy = library.assets.ingest_file(
        source,
        source=None,
        media_type=MediaType.OTHER,
        mime_type="text/plain",
    ).asset

    authoritative = legacy.validated_copy(
        update={
            "media_type": MediaType.VIDEO,
            "mime_type": "video/mp4",
            "width": 1080,
            "height": 1920,
            "duration_seconds": 2.0,
            "fps": 30.0,
            "has_audio": True,
        }
    )

    repository.enrich_asset(authoritative)
    # Re-applying the same authoritative result must also be idempotent.
    repository.enrich_asset(authoritative)

    repaired = library.database.get_asset(legacy.asset_id)
    assert repaired == authoritative
    assert repaired.sha256 == legacy.sha256
    assert repaired.size_bytes == legacy.size_bytes
    assert repaired.storage_key == legacy.storage_key
    assert repaired.created_at == legacy.created_at


def test_concurrent_equal_thumbnail_requests_publish_once(tmp_path, monkeypatch) -> None:
    library = LocalLibrary(tmp_path)
    source = tmp_path / "source.png"
    source.write_bytes(b"visual-source")
    asset = library.assets.ingest_file(
        source,
        source=None,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    ).asset
    source_path = library.assets.resolve(asset)

    call_count = 0
    count_lock = threading.Lock()

    def fake_run(arguments, **kwargs):
        nonlocal call_count
        with count_lock:
            call_count += 1
        # Widen the race enough that an implementation without publication locking
        # reliably lets both callers pass the initial missing-receipt check.
        time.sleep(0.05)
        Path(arguments[-1]).write_bytes(b"synthetic-jpeg")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(generate_thumbnail, library, asset, source_path)
            for _ in range(2)
        ]
        results = [future.result(timeout=5) for future in futures]

    assert call_count == 1
    assert results[0] is not None
    assert results[1] is not None
    assert results[0].storage_key == results[1].storage_key
    assert results[0].sha256 == results[1].sha256
    assert results[0].path.read_bytes() == b"synthetic-jpeg"

    slot = library.database.get_derivative_slot(asset.asset_id, "thumbnail.default")
    assert slot is not None
    assert slot.storage_key == results[0].storage_key
    assert slot.metadata["sha256"] == results[0].sha256
