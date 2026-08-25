from __future__ import annotations

import subprocess

import pytest

import content_forge.application.media as media_module
import content_forge.render.ffmpeg.probe as probe_module


_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_BOUNDED_FFPROBE_RUNNER = probe_module._run_ffprobe_bounded
_REAL_BOUNDED_THUMBNAIL_RUNNER = media_module._run_thumbnail_ffmpeg_bounded


@pytest.fixture(autouse=True)
def _bridge_legacy_subprocess_run_mocks(monkeypatch: pytest.MonkeyPatch):
    """Keep pre-hardening subprocess mocks on the new bounded-runner seams.

    A small number of older parser/thumbnail regressions supplied synthetic subprocess
    results by monkeypatching ``subprocess.run``. Production ffprobe and thumbnail FFmpeg
    now use bounded ``Popen`` runners so attacker-controlled diagnostics cannot be fully
    buffered. Bridge only an explicit legacy test monkeypatch into those new seams; normal
    tests and real FFmpeg integration continue through the actual bounded Popen paths.
    """

    def bridged_probe_runner(
        arguments: tuple[str, ...],
        *,
        timeout: float,
        stdout_limit: int = probe_module.FFPROBE_STDOUT_LIMIT_BYTES,
        stderr_limit: int = probe_module.FFPROBE_STDERR_LIMIT_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        if subprocess.run is not _REAL_SUBPROCESS_RUN:
            return subprocess.run(
                arguments,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        return _REAL_BOUNDED_FFPROBE_RUNNER(
            arguments,
            timeout=timeout,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
        )

    def bridged_thumbnail_runner(
        arguments: tuple[str, ...],
        *,
        timeout: float,
        stderr_limit: int = media_module.THUMBNAIL_STDERR_LIMIT_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        if subprocess.run is not _REAL_SUBPROCESS_RUN:
            try:
                return subprocess.run(
                    arguments,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    shell=False,
                )
            except OSError as exc:
                raise media_module.ThumbnailError(
                    "thumbnail execution could not start"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise media_module.ThumbnailError("thumbnail execution timed out") from exc
        return _REAL_BOUNDED_THUMBNAIL_RUNNER(
            arguments,
            timeout=timeout,
            stderr_limit=stderr_limit,
        )

    monkeypatch.setattr(probe_module, "_run_ffprobe_bounded", bridged_probe_runner)
    monkeypatch.setattr(
        media_module,
        "_run_thumbnail_ffmpeg_bounded",
        bridged_thumbnail_runner,
    )
