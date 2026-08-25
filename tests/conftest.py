from __future__ import annotations

import subprocess

import pytest

import content_forge.render.ffmpeg.probe as probe_module


_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_BOUNDED_FFPROBE_RUNNER = probe_module._run_ffprobe_bounded


@pytest.fixture(autouse=True)
def _bridge_legacy_ffprobe_run_mock(monkeypatch: pytest.MonkeyPatch):
    """Keep older probe parser tests on the bounded-runner test seam.

    One pre-existing parser regression monkeypatches ``probe_module.subprocess.run`` to
    supply synthetic ffprobe JSON. Production probing now uses ``Popen`` so output can be
    drained under hard byte budgets. Rather than weakening that production path, bridge
    only an explicit test monkeypatch of ``subprocess.run`` into the new runner seam.
    Normal tests and real FFmpeg integration still execute the actual bounded Popen path.
    """

    def bridged_runner(
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

    monkeypatch.setattr(probe_module, "_run_ffprobe_bounded", bridged_runner)
