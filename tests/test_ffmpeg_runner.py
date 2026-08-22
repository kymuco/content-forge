from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from content_forge.render.ffmpeg import (
    CancellationToken,
    FFmpegBackendError,
    RenderCommandManifest,
    execute_ffmpeg,
)


def manifest(output: Path, script: str) -> RenderCommandManifest:
    return RenderCommandManifest(
        render_plan_digest="a" * 64,
        ffmpeg_path=sys.executable,
        output_path=str(output),
        video_encoder="synthetic",
        filtergraph="synthetic",
        arguments=("-c", script, str(output)),
    )


def test_runner_returns_structured_success_for_nonempty_output(tmp_path: Path) -> None:
    output = tmp_path / "result.bin"
    command = manifest(
        output,
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'ok')",
    )

    result = execute_ffmpeg(command)

    assert result.output_path == str(output)
    assert result.bytes_written == 2
    assert result.return_code == 0


def test_runner_surfaces_structured_process_failure_and_removes_partial_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "partial.bin"
    command = manifest(
        output,
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'partial'); "
        "sys.stderr.write('synthetic failure'); sys.exit(7)",
    )

    with pytest.raises(FFmpegBackendError) as captured:
        execute_ffmpeg(command)

    assert captured.value.error.code == "ffmpeg_failed"
    assert captured.value.error.return_code == 7
    assert "synthetic failure" in (captured.value.error.stderr_tail or "")
    assert not output.exists()


def test_runner_cancellation_terminates_process_and_removes_partial_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "cancelled.bin"
    command = manifest(
        output,
        "from pathlib import Path; import sys, time; Path(sys.argv[1]).write_bytes(b'partial'); "
        "time.sleep(30)",
    )
    cancellation = CancellationToken()

    def cancel_soon() -> None:
        time.sleep(0.2)
        cancellation.cancel()

    thread = threading.Thread(target=cancel_soon, daemon=True)
    thread.start()
    with pytest.raises(FFmpegBackendError) as captured:
        execute_ffmpeg(command, cancellation=cancellation, poll_interval=0.05)
    thread.join(timeout=2)

    assert captured.value.error.code == "render_cancelled"
    assert not output.exists()


def test_runner_timeout_uses_distinct_error_code(tmp_path: Path) -> None:
    output = tmp_path / "timeout.bin"
    command = manifest(
        output,
        "from pathlib import Path; import sys, time; Path(sys.argv[1]).write_bytes(b'partial'); "
        "time.sleep(30)",
    )

    with pytest.raises(FFmpegBackendError) as captured:
        execute_ffmpeg(command, timeout=0.15, poll_interval=0.05)

    assert captured.value.error.code == "render_timeout"
    assert not output.exists()
