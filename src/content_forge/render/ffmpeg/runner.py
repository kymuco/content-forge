"""Cancellable FFmpeg process execution with structured failures."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .models import (
    RenderCommandManifest,
    RenderError,
    RenderResult,
    command_manifest_digest,
)


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class FFmpegBackendError(RuntimeError):
    def __init__(self, error: RenderError) -> None:
        super().__init__(error.message)
        self.error = error


def _error(
    manifest: RenderCommandManifest,
    *,
    code: str,
    stage: str,
    message: str,
    return_code: int | None = None,
    stderr: str | None = None,
) -> FFmpegBackendError:
    digest = command_manifest_digest(manifest)
    return FFmpegBackendError(
        RenderError(
            code=code,
            stage=stage,
            message=message,
            return_code=return_code,
            stderr_tail=None if not stderr else stderr[-16384:],
            manifest_digest=digest,
        )
    )


def _terminate(process: subprocess.Popen[str]) -> tuple[str, str]:
    process.terminate()
    try:
        return process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()


def _staging_path(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".rendering",
    )
    os.close(descriptor)
    return Path(name)


def execute_ffmpeg(
    manifest: RenderCommandManifest,
    *,
    cancellation: CancellationToken | None = None,
    timeout: float | None = None,
    poll_interval: float = 0.1,
) -> RenderResult:
    """Execute a command manifest and atomically publish only successful output.

    FFmpeg writes to a unique same-directory staging path. The requested destination is
    replaced only after FFmpeg exits successfully and the staging file is non-empty, so
    cancellation, timeout, startup failure, or encoder failure cannot erase a previous
    successful render.
    """

    destination = Path(manifest.output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    digest = command_manifest_digest(manifest)

    if cancellation is not None and cancellation.cancelled:
        raise _error(
            manifest,
            code="render_cancelled",
            stage="execute",
            message="render cancelled before process start",
        )

    try:
        staging = _staging_path(destination)
    except OSError as exc:
        raise _error(
            manifest,
            code="render_staging_failed",
            stage="execute",
            message=f"failed to create render staging file: {exc}",
        ) from exc

    # The manifest remains deterministic and names the final destination. Runtime
    # execution substitutes only the final output argument with a unique staging path;
    # all semantic/filter/codec arguments remain exactly those recorded in the manifest.
    execution_command = (*manifest.command[:-1], str(staging))

    try:
        process = subprocess.Popen(
            execution_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise _error(
            manifest,
            code="ffmpeg_start_failed",
            stage="execute",
            message=f"failed to start FFmpeg: {exc}",
        ) from exc

    stdout = ""
    stderr = ""
    try:
        while True:
            if cancellation is not None and cancellation.cancelled:
                stdout, stderr = _terminate(process)
                staging.unlink(missing_ok=True)
                raise _error(
                    manifest,
                    code="render_cancelled",
                    stage="execute",
                    message="render cancelled",
                    return_code=process.returncode,
                    stderr=stderr,
                )
            elapsed = time.monotonic() - started
            if timeout is not None and elapsed > timeout:
                stdout, stderr = _terminate(process)
                staging.unlink(missing_ok=True)
                raise _error(
                    manifest,
                    code="render_timeout",
                    stage="execute",
                    message=f"render exceeded timeout of {timeout:.3f}s",
                    return_code=process.returncode,
                    stderr=stderr,
                )
            try:
                stdout, stderr = process.communicate(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if process.poll() is None:
            _terminate(process)
        staging.unlink(missing_ok=True)
        raise

    elapsed = time.monotonic() - started
    if process.returncode != 0:
        staging.unlink(missing_ok=True)
        raise _error(
            manifest,
            code="ffmpeg_failed",
            stage="execute",
            message=f"FFmpeg exited with status {process.returncode}",
            return_code=process.returncode,
            stderr=stderr,
        )
    if not staging.is_file() or staging.stat().st_size <= 0:
        staging.unlink(missing_ok=True)
        raise _error(
            manifest,
            code="render_output_missing",
            stage="verify_output",
            message="FFmpeg reported success but produced no non-empty output file",
            return_code=process.returncode,
            stderr=stderr,
        )

    try:
        os.replace(staging, destination)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise _error(
            manifest,
            code="render_publish_failed",
            stage="publish_output",
            message=f"failed to publish completed render: {exc}",
            return_code=process.returncode,
            stderr=stderr,
        ) from exc

    # Flush the directory entry where practical so a successful return means the file is
    # visible to a following worker. Windows does not expose a portable directory fsync.
    if os.name != "nt":
        try:
            descriptor = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            descriptor = None
        if descriptor is not None:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    return RenderResult(
        output_path=str(destination),
        bytes_written=destination.stat().st_size,
        elapsed_seconds=elapsed,
        manifest_digest=digest,
        return_code=process.returncode,
    )
