from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import content_forge.application.media as media_module
from content_forge.application import ApplicationRepository
from content_forge.application.media import ThumbnailError, generate_thumbnail
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


def test_thumbnail_ffmpeg_stderr_overflow_is_bounded_generation_failure() -> None:
    limit = 8 * 1024
    script = (
        "import sys; "
        f"sys.stderr.buffer.write(b'x' * {limit + 1}); "
        "sys.stderr.buffer.flush()"
    )

    with pytest.raises(ThumbnailError, match="output exceeded safe limit"):
        media_module._run_thumbnail_ffmpeg_bounded(
            (sys.executable, "-c", script),
            timeout=5.0,
            stderr_limit=limit,
        )


def test_thumbnail_ffmpeg_discards_unused_stdout_without_capture_budget() -> None:
    # stdout is intentionally irrelevant to thumbnail generation. A child may write far
    # more than the diagnostic budget without consuming application memory or blocking.
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'x' * (1024 * 1024)); "
        "sys.stdout.buffer.flush()"
    )

    completed = media_module._run_thumbnail_ffmpeg_bounded(
        (sys.executable, "-c", script),
        timeout=5.0,
        stderr_limit=1024,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_thumbnail_interrupt_terminates_child_before_reader_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = threading.Event()

    class BlockingPipe:
        def read(self, size: int) -> bytes:
            assert size > 0
            assert released.wait(timeout=2.0), "reader was joined before child termination"
            return b""

        def close(self) -> None:
            pass

    class InterruptingProcess:
        def __init__(self) -> None:
            self.stderr = BlockingPipe()
            self.returncode: int | None = None
            self.killed = False
            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise SystemExit(17)
            assert self.killed
            self.returncode = -9
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            released.set()

    process = InterruptingProcess()
    monkeypatch.setattr(media_module.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(SystemExit) as exc_info:
        media_module._run_thumbnail_ffmpeg_bounded(("synthetic-ffmpeg",), timeout=60.0)

    assert exc_info.value.code == 17
    assert process.killed is True
    assert process.wait_calls == 2
    assert released.is_set()


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

    def fake_runner(arguments, *, timeout, stderr_limit=media_module.THUMBNAIL_STDERR_LIMIT_BYTES):
        nonlocal call_count
        with count_lock:
            call_count += 1
        protocol_index = arguments.index("-protocol_whitelist")
        assert arguments[protocol_index + 1] == "file"
        assert protocol_index < arguments.index("-i")
        # Widen the race enough that an implementation without publication locking
        # reliably lets both callers pass the initial missing-receipt check.
        time.sleep(0.05)
        Path(arguments[-1]).write_bytes(b"synthetic-jpeg")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(media_module, "_run_thumbnail_ffmpeg_bounded", fake_runner)

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
