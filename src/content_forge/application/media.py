"""Deterministic media-preparation helpers used by Inbox ingest."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

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

# FastAPI executes synchronous upload handlers in a thread pool. Equal source bytes share
# one canonical derivative path, so publication must be serialized even in the supported
# single-process server. Fixed stripes avoid an unbounded lock registry while ensuring a
# given storage key always maps to the same lock within the process.
_THUMBNAIL_PUBLICATION_LOCKS = tuple(threading.Lock() for _ in range(64))


def _thumbnail_publication_lock(storage_key: str) -> threading.Lock:
    return _THUMBNAIL_PUBLICATION_LOCKS[hash(storage_key) % len(_THUMBNAIL_PUBLICATION_LOCKS)]


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
            "-i",
            str(Path(source_path)),
            "-map",
            "0:v:0",
            "-vf",
            filtergraph,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(temporary),
        )
        try:
            try:
                completed = subprocess.run(
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
                # Failure to launch the configured FFmpeg binary is a media-preparation
                # configuration/generation failure, not evidence that thumbnail storage
                # itself is temporarily unavailable. Keep this catch scoped strictly to
                # subprocess launch so publication/fsync OSError still propagates to the
                # caller's post-acceptance operational retry policy.
                raise ThumbnailError("thumbnail execution could not start") from exc
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
        except subprocess.TimeoutExpired as exc:
            # A bounded FFmpeg execution/content-generation failure is a terminal media
            # preparation outcome for this intake. Filesystem/database OSError is
            # intentionally *not* converted here: the caller classifies those failures
            # as operational and keeps the FULL-accepted intake RECEIVING for retry.
            raise ThumbnailError(f"thumbnail execution timed out: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
