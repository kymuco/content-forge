"""ffprobe metadata extraction for local media."""

from __future__ import annotations

import json
import subprocess
import threading
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

from content_forge.core import Asset, MediaType

from .models import MediaProbe


class MediaProbeError(RuntimeError):
    pass


FFPROBE_STDOUT_LIMIT_BYTES = 4 * 1024 * 1024
FFPROBE_STDERR_LIMIT_BYTES = 256 * 1024
_FFPROBE_READ_CHUNK_BYTES = 64 * 1024
_LOCAL_MEDIA_PROTOCOL_WHITELIST = "file"
# Automatic Inbox media preparation intentionally supports only self-contained local
# media demuxers. Reference-bearing manifests/demuxers (HLS, DASH, concat, SDP, etc.) are
# absent so `file` can open the top-level canonical asset without granting that asset a
# generic local-filesystem traversal primitive through nested references.
#
# `mpegvideo` is included because FFmpeg 6.1.x may select the elementary MPEG-video
# demuxer while probing video carried by an otherwise self-contained MPEG-TS file.
_LOCAL_MEDIA_FORMAT_WHITELIST = (
    "aac,ac3,aiff,ape,avi,bmp_pipe,flac,flv,gif,image2,jpeg_pipe,matroska,"
    "mjpeg,mov,mp3,mpeg,mpegts,mpegvideo,ogg,opus,pam_pipe,pgm_pipe,pgmyuv_pipe,"
    "png_pipe,ppm_pipe,tiff_pipe,w64,wav,webm,webp_pipe"
)
_FFPROBE_SHOW_ENTRIES = (
    "format=format_name,duration:"
    "stream=codec_type,codec_name,duration,width,height,avg_frame_rate,r_frame_rate:"
    "stream_disposition=attached_pic"
)


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


def _is_attached_picture(stream: object) -> bool:
    """Return whether an ffprobe video stream is embedded artwork, not video content."""

    if not isinstance(stream, dict):
        return False
    disposition = stream.get("disposition")
    if not isinstance(disposition, dict):
        return False
    return disposition.get("attached_pic") in {1, True, "1"}


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Ensure a launched child cannot outlive an exceptional runner exit."""

    if process.poll() is None:
        _kill_process(process)
    try:
        process.wait()
    except Exception:
        # Cleanup must not replace the primary probe/capture exception. Control-flow
        # exceptions are deliberately not swallowed here.
        pass


def _run_ffprobe_bounded(
    arguments: tuple[str, ...],
    *,
    timeout: float,
    stdout_limit: int = FFPROBE_STDOUT_LIMIT_BYTES,
    stderr_limit: int = FFPROBE_STDERR_LIMIT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run ffprobe while imposing hard in-memory output bounds.

    `subprocess.run(..., stdout=PIPE, stderr=PIPE)` buffers both streams completely before
    returning. Media containers can carry attacker-controlled metadata and stream counts,
    so timeout and input-size limits alone do not bound that memory. Two readers drain the
    pipes concurrently, retain at most the configured byte budget, and terminate ffprobe
    immediately when either stream crosses its limit.
    """

    if stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("ffprobe output limits must be positive")

    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        raise MediaProbeError(f"ffprobe execution failed: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    output_exceeded = threading.Event()
    reader_errors: list[Exception] = []
    reader_error_lock = threading.Lock()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def drain(stream: BinaryIO, limit: int, chunks: list[bytes]) -> None:
        total = 0
        try:
            while True:
                chunk = stream.read(_FFPROBE_READ_CHUNK_BYTES)
                if not chunk:
                    break
                remaining = limit - total
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                total += len(chunk)
                if total > limit:
                    output_exceeded.set()
                    _kill_process(process)
                    # Continue draining until EOF so the child cannot remain blocked on a
                    # full pipe while termination is being delivered.
        except Exception as exc:  # pragma: no cover - defensive OS/pipe failure boundary
            with reader_error_lock:
                reader_errors.append(exc)
            _kill_process(process)

    stdout_reader = threading.Thread(
        target=drain,
        args=(process.stdout, stdout_limit, stdout_chunks),
        name="content-forge-ffprobe-stdout",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=drain,
        args=(process.stderr, stderr_limit, stderr_chunks),
        name="content-forge-ffprobe-stderr",
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()

    timed_out = False
    try:
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process(process)
            returncode = process.wait()
    finally:
        # `KeyboardInterrupt`, `SystemExit`, and any other exceptional exit can arrive
        # while wait() is blocked. Terminate/reap before joining pipe readers so shutdown
        # propagation can never wait indefinitely for a still-running child to close EOF.
        _terminate_and_reap(process)
        stdout_reader.join()
        stderr_reader.join()
        process.stdout.close()
        process.stderr.close()

    if timed_out:
        raise MediaProbeError("ffprobe execution timed out")
    if reader_errors:
        raise MediaProbeError("ffprobe output capture failed") from reader_errors[0]
    if output_exceeded.is_set():
        raise MediaProbeError("ffprobe output exceeded safe limit")

    return subprocess.CompletedProcess(
        arguments,
        returncode,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )


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
        "-protocol_whitelist",
        _LOCAL_MEDIA_PROTOCOL_WHITELIST,
        "-format_whitelist",
        _LOCAL_MEDIA_FORMAT_WHITELIST,
        "-show_entries",
        _FFPROBE_SHOW_ENTRIES,
        "-of",
        "json",
        str(source),
    )
    result = _run_ffprobe_bounded(arguments, timeout=timeout)

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
    # Audio containers commonly expose embedded album art as a video stream with
    # disposition.attached_pic=1. It is metadata, not timeline video, and must not turn
    # MP3/M4A assets into VIDEO or leak artwork dimensions into shared media metadata.
    video_stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict)
            and item.get("codec_type") == "video"
            and not _is_attached_picture(item)
        ),
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
