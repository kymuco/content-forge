"""Small deterministic media-preparation helpers used by Inbox ingest."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from content_forge.core import Asset, MediaType
from content_forge.storage import DerivativeSlot, LocalLibrary, sha256_file


class ThumbnailError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    storage_key: str
    path: Path
    sha256: str
    size_bytes: int


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
    if width < 1 or height < 1:
        raise ValueError("thumbnail dimensions must be positive")

    storage_key = (
        f"thumbnails/sha256/{asset.sha256[:2]}/{asset.sha256[2:4]}/"
        f"{asset.sha256}-v1-{width}x{height}.jpg"
    )
    output = library.paths.root / storage_key
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_file() and output.stat().st_size > 0:
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ThumbnailError(f"thumbnail execution failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
