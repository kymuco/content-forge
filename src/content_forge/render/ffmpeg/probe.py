"""ffprobe metadata extraction for local media."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

from content_forge.core import Asset, MediaType

from .models import MediaProbe


class MediaProbeError(RuntimeError):
    pass


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0.0 else None


def _fps(value: object) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        parsed = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return parsed if parsed > 0.0 else None


def probe_media(
    path: str | Path,
    *,
    ffprobe_path: str = "ffprobe",
    timeout: float = 20.0,
) -> MediaProbe:
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(source)

    arguments = (
        ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(source),
    )
    try:
        result = subprocess.run(
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaProbeError(f"ffprobe execution failed: {exc}") from exc

    if result.returncode != 0:
        message = result.stderr.strip()
        raise MediaProbeError(
            f"ffprobe failed ({result.returncode}): {message[-4000:]}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe returned invalid JSON") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list):
        streams = []
    video_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"),
        None,
    )
    format_info = payload.get("format")
    if not isinstance(format_info, dict):
        format_info = {}

    duration = _positive_float(format_info.get("duration"))
    if duration is None:
        durations = [
            value
            for item in streams
            if isinstance(item, dict)
            for value in [_positive_float(item.get("duration"))]
            if value is not None
        ]
        duration = max(durations) if durations else None

    width = None
    height = None
    fps = None
    video_codec = None
    if isinstance(video_stream, dict):
        raw_width = video_stream.get("width")
        raw_height = video_stream.get("height")
        if isinstance(raw_width, int) and raw_width > 0:
            width = raw_width
        if isinstance(raw_height, int) and raw_height > 0:
            height = raw_height
        fps = _fps(video_stream.get("avg_frame_rate")) or _fps(
            video_stream.get("r_frame_rate")
        )
        codec = video_stream.get("codec_name")
        if isinstance(codec, str) and codec:
            video_codec = codec

    audio_codec = None
    if isinstance(audio_stream, dict):
        codec = audio_stream.get("codec_name")
        if isinstance(codec, str) and codec:
            audio_codec = codec

    format_name = format_info.get("format_name")
    if not isinstance(format_name, str) or not format_name:
        format_name = None

    return MediaProbe(
        path=str(source.resolve()),
        format_name=format_name,
        duration_seconds=duration,
        width=width,
        height=height,
        fps=fps,
        has_video=video_stream is not None,
        has_audio=audio_stream is not None,
        video_codec=video_codec,
        audio_codec=audio_codec,
    )


def apply_probe_to_asset(asset: Asset, probe: MediaProbe) -> Asset:
    """Return a validated Asset copy enriched with probe metadata.

    The function does not persist anything; storage/database policy remains owned by the
    caller. Media-type contradictions fail before metadata is accepted.
    """

    if asset.media_type in {MediaType.VIDEO, MediaType.IMAGE} and not probe.has_video:
        raise MediaProbeError("visual asset probe contains no video/image stream")
    if asset.media_type is MediaType.AUDIO and not probe.has_audio:
        raise MediaProbeError("audio asset probe contains no audio stream")

    duration = probe.duration_seconds
    if asset.media_type is MediaType.IMAGE:
        duration = None

    return asset.validated_copy(
        update={
            "width": probe.width if probe.has_video else None,
            "height": probe.height if probe.has_video else None,
            "duration_seconds": duration,
            "fps": probe.fps if asset.media_type is MediaType.VIDEO else None,
            "has_audio": probe.has_audio,
        }
    )
