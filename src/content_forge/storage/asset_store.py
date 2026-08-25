"""Content-addressed local media storage."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from content_forge.core import Asset, MediaType, SourceRecord

from .database import LibraryDatabase, StorageError
from .paths import RuntimePaths, fsync_directory_chain
from .records import SourceInput

CHUNK_SIZE = 1024 * 1024


class AssetIntegrityError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class IngestResult:
    asset: Asset
    source_record: SourceRecord | None
    deduplicated: bool
    blob_path: Path


def _media_type_for_mime(mime_type: str) -> MediaType:
    if mime_type.startswith("video/"):
        return MediaType.VIDEO
    if mime_type.startswith("image/"):
        return MediaType.IMAGE
    if mime_type.startswith("audio/"):
        return MediaType.AUDIO
    return MediaType.OTHER


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


class AssetStore:
    """Owns immutable blob bytes while `LibraryDatabase` owns their metadata."""

    def __init__(self, paths: RuntimePaths, database: LibraryDatabase) -> None:
        self.paths = paths.ensure()
        self.database = database

    def ingest_file(
        self,
        path: str | Path,
        *,
        source: SourceInput | None = None,
        media_type: MediaType | None = None,
        mime_type: str | None = None,
    ) -> IngestResult:
        input_path = Path(path)
        if not input_path.is_file():
            raise FileNotFoundError(input_path)

        guessed_mime, _ = mimetypes.guess_type(input_path.name)
        resolved_mime = mime_type or guessed_mime or "application/octet-stream"
        resolved_media_type = media_type or _media_type_for_mime(resolved_mime)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.paths.incoming,
            prefix="ingest-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size_bytes = 0

        try:
            # Own the staging descriptor before attempting to open the source. If the
            # source disappears or becomes unreadable after the initial is_file() check,
            # the destination context still closes the descriptor on every platform.
            with os.fdopen(descriptor, "wb") as destination_handle, input_path.open(
                "rb"
            ) as source_handle:
                while chunk := source_handle.read(CHUNK_SIZE):
                    digest.update(chunk)
                    size_bytes += len(chunk)
                    destination_handle.write(chunk)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())

            sha256 = digest.hexdigest()
            blob_path = self.paths.blob_path_for_sha256(sha256)
            storage_key = self.paths.storage_key_for_sha256(sha256)
            blob_path.parent.mkdir(parents=True, exist_ok=True)

            existing = self.database.get_asset_by_sha256(sha256)
            if blob_path.exists():
                if blob_path.stat().st_size != size_bytes or sha256_file(blob_path) != sha256:
                    raise AssetIntegrityError(
                        f"content-addressed blob is corrupt: {blob_path}"
                    )
                temporary.unlink()
            else:
                os.replace(temporary, blob_path)

            # A canonical pathname can be present after an earlier atomic rename even if
            # that operation's directory fsync failed. Re-establish the durability
            # barrier on every successful ingest/reuse before any catalog or provenance
            # receipt is allowed to advance. This is idempotent on POSIX and a documented
            # no-op on Windows where Python has no portable directory-fsync primitive.
            fsync_directory_chain(blob_path.parent, stop_at=self.paths.root)

            if existing is not None:
                if existing.size_bytes != size_bytes:
                    raise AssetIntegrityError(
                        f"stored metadata size disagrees with digest {sha256}"
                    )
                if existing.storage_key not in {None, storage_key}:
                    raise AssetIntegrityError(
                        f"stored key disagrees with canonical key for digest {sha256}"
                    )
                asset = existing
                deduplicated = True
            else:
                candidate = Asset(
                    sha256=sha256,
                    media_type=resolved_media_type,
                    mime_type=resolved_mime,
                    size_bytes=size_bytes,
                    storage_key=storage_key,
                )
                asset = self.database.put_asset(candidate)
                deduplicated = asset.asset_id != candidate.asset_id

            source_record = None
            if source is not None:
                source_record = SourceRecord(
                    asset_id=asset.asset_id,
                    **source.model_dump(),
                )
                self.database.add_source(source_record)

            return IngestResult(
                asset=asset,
                source_record=source_record,
                deduplicated=deduplicated,
                blob_path=blob_path,
            )
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def resolve(self, asset: Asset) -> Path:
        expected_key = self.paths.storage_key_for_sha256(asset.sha256)
        if asset.storage_key not in {None, expected_key}:
            raise AssetIntegrityError(
                f"asset storage key is not canonical for {asset.asset_id}"
            )
        path = self.paths.blob_path_for_sha256(asset.sha256)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def verify(self, asset: Asset) -> bool:
        """Verify canonical bytes and reassert their directory-entry durability."""

        path = self.resolve(asset)
        verified = path.stat().st_size == asset.size_bytes and sha256_file(path) == asset.sha256
        if verified:
            # Recovery may encounter a pathname left behind by a rename whose directory
            # fsync failed just before process interruption. Do not let a verified name be
            # treated as authoritative until that durability barrier succeeds again.
            fsync_directory_chain(path.parent, stop_at=self.paths.root)
        return verified
