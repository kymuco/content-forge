"""FFmpeg/ffprobe discovery and runtime capability probing."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .models import FFmpegCapabilities

_ENCODER_RE = re.compile(r"^\s*[A-Z\.]{6,}\s+([^\s]+)")
_FILTER_RE = re.compile(r"^\s*[TSC\.]{3,}\s+([^\s]+)")


class FFmpegCapabilityError(RuntimeError):
    pass


def _resolve_binary(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        if not candidate.is_file():
            raise FFmpegCapabilityError(f"binary does not exist: {candidate}")
        return str(candidate.resolve())
    located = shutil.which(value)
    if located is None:
        raise FFmpegCapabilityError(f"binary not found on PATH: {value}")
    return str(Path(located).resolve())


def _capture(arguments: Sequence[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            tuple(arguments),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FFmpegCapabilityError(f"failed to execute {arguments[0]}: {exc}") from exc


def _require_success(result: subprocess.CompletedProcess[str], stage: str) -> str:
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise FFmpegCapabilityError(f"{stage} failed ({result.returncode}): {message[-2000:]}")
    return result.stdout + "\n" + result.stderr


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        value = line.strip()
        if value:
            return value
    raise FFmpegCapabilityError("version command returned no text")


def parse_encoders(text: str) -> tuple[str, ...]:
    names: set[str] = set()
    for line in text.splitlines():
        match = _ENCODER_RE.match(line)
        if match:
            names.add(match.group(1))
    return tuple(sorted(names))


def parse_filters(text: str) -> tuple[str, ...]:
    names: set[str] = set()
    for line in text.splitlines():
        match = _FILTER_RE.match(line)
        if match:
            names.add(match.group(1))
    return tuple(sorted(names))


def _probe_nvenc(ffmpeg_path: str, *, timeout: float) -> tuple[bool, str | None]:
    result = _capture(
        (
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=1:d=0.1",
            "-frames:v",
            "1",
            "-c:v",
            "h264_nvenc",
            "-f",
            "null",
            "-",
        ),
        timeout=timeout,
    )
    if result.returncode == 0:
        return True, None
    error = (result.stderr or result.stdout).strip()
    return False, error[-2000:] or "NVENC probe failed without diagnostic output"


def probe_ffmpeg_runtime(
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    *,
    test_nvenc: bool = True,
    timeout: float = 10.0,
) -> FFmpegCapabilities:
    """Resolve binaries and inspect encoder/filter/runtime support.

    NVENC is considered usable only when FFmpeg both advertises `h264_nvenc` and a
    one-frame hardware encode succeeds. This prevents a build-time encoder listing from
    being mistaken for an available NVIDIA device/driver. CPU `libx264` remains the
    deterministic fallback.
    """

    ffmpeg_path = _resolve_binary(ffmpeg)
    ffprobe_path = _resolve_binary(ffprobe)

    ffmpeg_version_text = _require_success(
        _capture((ffmpeg_path, "-hide_banner", "-version"), timeout=timeout),
        "ffmpeg version probe",
    )
    ffprobe_version_text = _require_success(
        _capture((ffprobe_path, "-hide_banner", "-version"), timeout=timeout),
        "ffprobe version probe",
    )
    encoder_text = _require_success(
        _capture((ffmpeg_path, "-hide_banner", "-encoders"), timeout=timeout),
        "ffmpeg encoder probe",
    )
    filter_text = _require_success(
        _capture((ffmpeg_path, "-hide_banner", "-filters"), timeout=timeout),
        "ffmpeg filter probe",
    )

    encoders = parse_encoders(encoder_text)
    filters = parse_filters(filter_text)
    nvenc_usable = False
    nvenc_error = None
    if "h264_nvenc" in encoders:
        if test_nvenc:
            nvenc_usable, nvenc_error = _probe_nvenc(ffmpeg_path, timeout=timeout)
        else:
            nvenc_error = "NVENC runtime probe skipped"

    return FFmpegCapabilities(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        ffmpeg_version=_first_nonempty_line(ffmpeg_version_text),
        ffprobe_version=_first_nonempty_line(ffprobe_version_text),
        encoders=encoders,
        filters=filters,
        h264_nvenc_usable=nvenc_usable,
        nvenc_probe_error=nvenc_error,
    )


def select_h264_encoder(
    capabilities: FFmpegCapabilities,
    *,
    prefer_nvenc: bool = True,
) -> str:
    if prefer_nvenc and capabilities.h264_nvenc_usable:
        return "h264_nvenc"
    if capabilities.has_libx264:
        return "libx264"
    if capabilities.h264_nvenc_usable:
        return "h264_nvenc"
    raise FFmpegCapabilityError(
        "no usable H.264 encoder: NVENC unavailable and libx264 not present"
    )


def require_filters(capabilities: FFmpegCapabilities, names: set[str]) -> None:
    missing = sorted(names.difference(capabilities.filters))
    if missing:
        raise FFmpegCapabilityError(
            "FFmpeg build is missing required filters: " + ", ".join(missing)
        )
