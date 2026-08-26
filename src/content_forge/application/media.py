"""Deterministic media-preparation helpers used by Inbox ingest."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from content_forge.core import Asset, MediaType
from content_forge.render.ffmpeg import MediaProbe, apply_probe_to_asset
from content_forge.storage import DerivativeSlot, LocalLibrary, sha256_file
from content_forge.storage.paths import fsync_directory_chain


class ThumbnailError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    storage_key: str
    path: Path
    sha256: str
    size_bytes: int


IMAGE_FORMAT_TOKENS = {
    "image2",
    "image2pipe",
    "png_pipe",
    "jpeg_pipe",
    "webp_pipe",
    "bmp_pipe",
    "tiff_pipe",
    "gif",
}

THUMBNAIL_STDERR_LIMIT_BYTES = 256 * 1024
_THUMBNAIL_STDERR_READ_CHUNK_BYTES = 64 * 1024
_LOCAL_MEDIA_PROTOCOL_WHITELIST = "file"
# Keep this in lockstep with the automatic ffprobe policy. Reference-bearing manifest
# demuxers such as HLS/DASH/concat/SDP are intentionally absent so the allowed top-level
# `file` protocol cannot be repurposed to read arbitrary nested local paths. `mpegvideo`
# remains allowed because FFmpeg 6.1.x may select that self-contained elementary demuxer
# while consuming video carried by MPEG-TS.
_LOCAL_MEDIA_FORMAT_WHITELIST = (
    "aac,ac3,aiff,ape,avi,bmp_pipe,flac,flv,gif,image2,jpeg_pipe,matroska,"
    "mjpeg,mov,mp3,mpeg,mpegts,mpegvideo,ogg,opus,pam_pipe,pgm_pipe,pgmyuv_pipe,"
    "png_pipe,ppm_pipe,tiff_pipe,w64,wav,webm,webp_pipe"
)

# FastAPI executes synchronous upload handlers in a thread pool. Equal source bytes share
# one canonical derivative path, so publication must be serialized even in the supported
# single-process server. Fixed stripes avoid an unbounded lock registry while ensuring a
# given storage key always maps to the same lock within the process.
_THUMBNAIL_PUBLICATION_LOCKS = tuple(threading.Lock() for _ in range(64))


def _thumbnail_publication_lock(storage_key: str) -> threading.Lock:
    return _THUMBNAIL_PUBLICATION_LOCKS[hash(storage_key) % len(_THUMBNAIL_PUBLICATION_LOCKS)]


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Ensure a launched thumbnail child cannot outlive an exceptional runner exit."""

    if process.poll() is None:
        _kill_process(process)
    try:
        process.wait()
    except Exception:
        # Cleanup must not replace the primary thumbnail/capture exception. Control-flow
        # exceptions are deliberately not swallowed here.
        pass


def _run_thumbnail_ffmpeg_bounded(
    arguments: tuple[str, ...],
    *,
    timeout: float,
    stderr_limit: int = THUMBNAIL_STDERR_LIMIT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run thumbnail FFmpeg without buffering attacker-controlled diagnostics.

    Thumbnail generation never consumes stdout, so it is sent directly to DEVNULL. FFmpeg
    stderr can grow rapidly for malformed media even under a wall-clock timeout, therefore
    one reader drains it concurrently while retaining only a hard byte budget. Crossing the
    budget terminates FFmpeg immediately and is classified as a bounded generation failure.
    """

    if stderr_limit < 1:
        raise ValueError("thumbnail stderr limit must be positive")

    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        # Keep launch/configuration failures distinct from later filesystem publication
        # failures. Callers intentionally treat this as a terminal thumbnail outcome.
        raise ThumbnailError("thumbnail execution could not start") from exc

    assert process.stderr is not None
    output_exceeded = threading.Event()
    reader_errors: list[Exception] = []
    stderr_chunks: list[bytes] = []

    def drain_stderr(stream: BinaryIO) -> None:
        total = 0
        try:
            while True:
                chunk = stream.read(_THUMBNAIL_STDERR_READ_CHUNK_BYTES)
                if not chunk:
                    break
                remaining = stderr_limit - total
                if remaining > 0:
                    stderr_chunks.append(chunk[:remaining])
                total += len(chunk)
                if total > stderr_limit:
                    output_exceeded.set()
                    _kill_process(process)
                    # Continue draining until EOF while process termination is delivered.
        except Exception as exc:  # pragma: no cover - defensive OS/pipe boundary
            reader_errors.append(exc)
            _kill_process(process)

    stderr_reader = threading.Thread(
        target=drain_stderr,
        args=(process.stderr,),
        name="content-forge-thumbnail-stderr",
        daemon=True,
    )
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
        # A shutdown/control-flow exception can interrupt wait() itself. Reap the child
        # before joining the stderr reader so propagation cannot block waiting for EOF.
        _terminate_and_reap(process)
        stderr_reader.join()
        process.stderr.close()

    if timed_out:
        raise ThumbnailError("thumbnail execution timed out")
    if reader_errors:
        raise ThumbnailError("thumbnail output capture failed") from reader_errors[0]
    if output_exceeded.is_set():
        raise ThumbnailError("thumbnail output exceeded safe limit")

    return subprocess.CompletedProcess(
        arguments,
        returncode,
        stdout="",
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )


def authoritative_media_classification(probe: MediaProbe) -> tuple[MediaType, str]:
    """Classify shared Asset metadata from ffprobe facts, never client headers."""

    tokens = {
        token.strip().lower()
        for token in (probe.format_name or "").split(",")
        if token.strip()
    }
    codec = (probe.video_codec or "").lower()

    if tokens & IMAGE_FORMAT_TOKENS:
        media_type = MediaType.IMAGE
    elif probe.has_video:
        media_type = MediaType.VIDEO
    elif probe.has_audio:
        media_type = MediaType.AUDIO
    else:
        media_type = MediaType.OTHER

    if media_type is MediaType.IMAGE:
        if codec == "png" or "png_pipe" in tokens:
            return media_type, "image/png"
        if codec in {"mjpeg", "jpeg"} or "jpeg_pipe" in tokens:
            return media_type, "image/jpeg"
        if codec == "webp" or "webp_pipe" in tokens:
            return media_type, "image/webp"
        if codec == "gif" or "gif" in tokens:
            return media_type, "image/gif"
        if codec == "bmp" or "bmp_pipe" in tokens:
            return media_type, "image/bmp"
        if codec.startswith("tiff") or "tiff_pipe" in tokens:
            return media_type, "image/tiff"
        return media_type, "application/octet-stream"

    if media_type is MediaType.VIDEO:
        if tokens & {"mp4", "mov", "m4a", "3gp", "3g2", "mj2"}:
            return media_type, "video/mp4"
        if "webm" in tokens:
            return media_type, "video/webm"
        if "matroska" in tokens:
            return media_type, "video/x-matroska"
        if "avi" in tokens:
            return media_type, "video/x-msvideo"
        if "mpegts" in tokens:
            return media_type, "video/mp2t"
        return media_type, "application/octet-stream"

    if media_type is MediaType.AUDIO:
        if "mp3" in tokens:
            return media_type, "audio/mpeg"
        if "wav" in tokens:
            return media_type, "audio/wav"
        if "flac" in tokens:
            return media_type, "audio/flac"
        if "ogg" in tokens:
            return media_type, "audio/ogg"
        if tokens & {"mp4", "mov", "m4a"}:
            return media_type, "audio/mp4"
        return media_type, "application/octet-stream"

    return MediaType.OTHER, "application/octet-stream"


def apply_authoritative_probe(asset: Asset, probe: MediaProbe) -> Asset:
    media_type, mime_type = authoritative_media_classification(probe)
    classified = asset.validated_copy(
        update={"media_type": media_type, "mime_type": mime_type}
    )
    return apply_probe_to_asset(classified, probe)


def thumbnail_storage_key(
    asset: Asset,
    *,
    width: int = 360,
    height: int = 640,
) -> str:
    if width < 1 or height < 1:
        raise ValueError("thumbnail dimensions must be positive")
    return (
        f"thumbnails/sha256/{asset.sha256[:2]}/{asset.sha256[2:4]}/"
        f"{asset.sha256}-v1-{width}x{height}.jpg"
    )


def _record_thumbnail(
    library: LocalLibrary,
    asset: Asset,
    *,
    storage_key: str,
    output: Path,
    width: int,
    height: int,
) -> ThumbnailResult:
    digest = sha256_file(output)
    result = ThumbnailResult(storage_key, output, digest, output.stat().st_size)
    library.database.put_derivative_slot(
        DerivativeSlot(
            asset_id=asset.asset_id,
            slot="thumbnail.default",
            storage_key=storage_key,
            metadata={
                "mime_type": "image/jpeg",
                "sha256": digest,
                "size_bytes": result.size_bytes,
                "max_width": width,
                "max_height": height,
                "source_sha256": asset.sha256,
            },
        )
    )
    return result


def _existing_thumbnail_is_authenticated(
    library: LocalLibrary,
    asset: Asset,
    *,
    storage_key: str,
    output: Path,
) -> bool:
    if not output.is_file() or output.stat().st_size <= 0:
        return False
    slot = library.database.get_derivative_slot(asset.asset_id, "thumbnail.default")
    if slot is None or slot.storage_key != storage_key:
        return False
    metadata = slot.metadata
    digest = metadata.get("sha256")
    source_digest = metadata.get("source_sha256")
    return (
        isinstance(digest, str)
        and len(digest) == 64
        and source_digest == asset.sha256
        and sha256_file(output) == digest
    )


def generate_thumbnail(
    library: LocalLibrary,
    asset: Asset,
    source_path: str | Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    width: int = 360,
    height: int = 640,
    timeout: float = 30.0,
) -> ThumbnailResult | None:
    if asset.media_type not in {MediaType.VIDEO, MediaType.IMAGE}:
        return None

    storage_key = thumbnail_storage_key(asset, width=width, height=height)
    output = library.paths.root / storage_key
    output.parent.mkdir(parents=True, exist_ok=True)

    # The check must be repeated *inside* the publication lock. Otherwise two equal
    # uploads can both observe a missing receipt, one publish, and the other unlink the
    # just-published canonical file before its receipt is recorded.
    with _thumbnail_publication_lock(storage_key):
        if _existing_thumbnail_is_authenticated(
            library,
            asset,
            storage_key=storage_key,
            output=output,
        ):
            return ThumbnailResult(
                storage_key,
                output,
                sha256_file(output),
                output.stat().st_size,
            )

        output.unlink(missing_ok=True)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.stem}-",
            suffix=".jpg",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink(missing_ok=True)
        filtergraph = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2"
        )
        arguments = (
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            _LOCAL_MEDIA_PROTOCOL_WHITELIST,
            "-format_whitelist",
            _LOCAL_MEDIA_FORMAT_WHITELIST,
            "-i",
            str(Path(source_path)),
            "-map",
            # Uppercase V intentionally selects only real video streams. Lowercase v
            # also includes attached pictures/cover art and can choose an embedded cover
            # before the authoritative non-attached video stream selected by ffprobe.
            "0:V:0",
            "-vf",
            filtergraph,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(temporary),
        )
        try:
            completed = _run_thumbnail_ffmpeg_bounded(arguments, timeout=timeout)
            if completed.returncode != 0:
                raise ThumbnailError(
                    f"ffmpeg thumbnail failed ({completed.returncode}): "
                    f"{completed.stderr.strip()[-4000:]}"
                )
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise ThumbnailError("ffmpeg did not produce a non-empty thumbnail")
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, output)
            # The derivative receipt must never commit before the canonical rename and
            # any newly-created thumbnail shard directories are durable.
            fsync_directory_chain(output.parent, stop_at=library.paths.root)
            return _record_thumbnail(
                library,
                asset,
                storage_key=storage_key,
                output=output,
                width=width,
                height=height,
            )
        finally:
            # Publication/storage OSError intentionally remains unwrapped so the caller's
            # post-acceptance operational policy can preserve RECEIVING and retry later.
            temporary.unlink(missing_ok=True)
