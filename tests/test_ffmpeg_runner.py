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


def test_runner_failure_preserves_previous_successful_output(tmp_path: Path) -> None:
    output = tmp_path / "previous.bin"
    output.write_bytes(b"previous-success")
    command = manifest(
        output,
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'partial'); "
        "sys.exit(9)",
    )

    with pytest.raises(FFmpegBackendError) as captured:
        execute_ffmpeg(command)

    assert captured.value.error.code == "ffmpeg_failed"
    assert output.read_bytes() == b"previous-success"
    assert not list(tmp_path.glob(f".{output.name}.*.rendering"))


def test_runner_success_replaces_previous_output_only_after_completion(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replace.bin"
    output.write_bytes(b"old")
    command = manifest(
        output,
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'new')",
    )

    result = execute_ffmpeg(command)

    assert result.bytes_written == 3
    assert output.read_bytes() == b"new"
    assert not list(tmp_path.glob(f".{output.name}.*.rendering"))


def test_runner_precancel_preserves_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "already-rendered.bin"
    output.write_bytes(b"completed")
    command = manifest(
        output,
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'new')",
    )
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(FFmpegBackendError) as captured:
        execute_ffmpeg(command, cancellation=cancellation)

    assert captured.value.error.code == "render_cancelled"
    assert output.read_bytes() == b"completed"
    assert not list(tmp_path.glob(f".{output.name}.*.rendering"))


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
    assert not list(tmp_path.glob(f".{output.name}.*.rendering"))


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
    assert not list(tmp_path.glob(f".{output.name}.*.rendering"))


def test_runner_timeout_is_enforced_when_process_exits_inside_long_poll(
    tmp_path: Path,
) -> None:
    output = tmp_path / "late.bin"
    output.write_bytes(b"previous")
    command = manifest(
        output,
        "from pathlib import Path; import sys, time; time.sleep(0.12); "
        "Path(sys.argv[1]).write_bytes(b'late-success')",
    )

    with pytest.raises(FFmpegBackendError) as captured:
        execute_ffmpeg(command, timeout=0.05, poll_interval=0.5)

    assert captured.value.error.code == "render_timeout"
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob(f".{output.name}.*.rendering"))


def test_runner_popen_value_error_is_structured_and_cleans_staging(tmp_path: Path) -> None:
    output = tmp_path / "nul.bin"
    output.write_bytes(b"previous")
    command = manifest(output, "print('bad')\x00")

    with pytest.raises(FFmpegBackendError) as captured:
        execute_ffmpeg(command)

    assert captured.value.error.code == "ffmpeg_start_failed"
    assert "embedded null" in captured.value.error.message.lower()
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob(f".{output.name}.*.rendering"))
