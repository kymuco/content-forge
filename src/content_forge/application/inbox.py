"""Durable Inbox application service used by API, future PWA, and future CLI."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from content_forge.core import AssetRef, MediaType, Project, ProjectState
from content_forge.render.ffmpeg import MediaProbeError, apply_probe_to_asset, probe_media
from content_forge.storage import LocalLibrary, SourceInput, sha256_file

from .media import ThumbnailError, generate_thumbnail, thumbnail_storage_key
from .models import (
    InboxIntake,
    IntakeKind,
    IntakeState,
    PreparationState,
)
from .repository import ApplicationRepository


class InboxError(RuntimeError):
    pass


class UploadTooLargeError(InboxError):
    pass


class InboxService:
    def __init__(
        self,
        library: LocalLibrary,
        repository: ApplicationRepository,
        *,
        ffprobe_path: str = "ffprobe",
        ffmpeg_path: str = "ffmpeg",
        max_upload_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        self.library = library
        self.repository = repository
        self.ffprobe_path = ffprobe_path
        self.ffmpeg_path = ffmpeg_path
        self.max_upload_bytes = max_upload_bytes

    def list_intakes(self, *, limit: int = 100) -> tuple[InboxIntake, ...]:
        return self.repository.list_intakes(limit=limit)

    def get_intake(self, intake_id: str) -> InboxIntake | None:
        return self.repository.get_intake(intake_id)

    def ingest_upload(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        mime_type: str | None = None,
        source_url: str | None = None,
        note: str | None = None,
        creator_hint: str | None = None,
        content_kind_hint: str | None = None,
    ) -> InboxIntake:
        intake = self.repository.create_intake(
            InboxIntake(
                kind=IntakeKind.FILE,
                original_name=filename,
                mime_type=mime_type,
                source_url=source_url,
                note=note,
                creator_hint=creator_hint,
                content_kind_hint=content_kind_hint,
            )
        )
        descriptor: int | None = None
        staged: Path | None = None
        size_bytes = 0
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.library.paths.incoming,
                prefix=f"http-{intake.intake_id}-",
                suffix=Path(filename).suffix or ".upload",
            )
            staged = Path(temporary_name)
            handle = os.fdopen(descriptor, "wb")
            descriptor = None
            with handle:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.max_upload_bytes:
                        raise UploadTooLargeError(
                            f"upload exceeds {self.max_upload_bytes} bytes"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            source = SourceInput(
                source_url=source_url,
                creator_name=creator_hint,
                original_title=filename,
                notes=note,
            )
            ingest_mime = (
                None if mime_type in {None, "application/octet-stream"} else mime_type
            )
            result = self.library.assets.ingest_file(
                staged,
                source=source,
                mime_type=ingest_mime,
            )
            asset = result.asset
            source_id = (
                None if result.source_record is None else result.source_record.source_id
            )

            # First durable checkpoint: immutable bytes and provenance already exist.
            self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.RECEIVING,
                    "size_bytes": size_bytes,
                    "asset_id": asset.asset_id,
                    "source_id": source_id,
                },
            )

            ref = AssetRef(asset_id=asset.asset_id, source_id=source_id)
            project = Project(
                content_kind=content_kind_hint or "unclassified",
                state=ProjectState.INBOX,
                source_refs=(ref,),
                source_records=(
                    () if result.source_record is None else (result.source_record,)
                ),
                metadata={
                    "inbox_intake_id": intake.intake_id,
                    "original_filename": filename,
                },
            )
            self.library.save_project(project)

            # Second durable checkpoint: later preparation failures retain project link.
            self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.RECEIVING,
                    "project_id": project.project_id,
                },
            )

            probe_state = PreparationState.PENDING
            thumbnail_state = PreparationState.PENDING
            error_code: str | None = None
            error_message: str | None = None

            try:
                probe = probe_media(result.blob_path, ffprobe_path=self.ffprobe_path)
                asset = apply_probe_to_asset(asset, probe)
                self.repository.enrich_asset(asset)
                probe_state = PreparationState.SUCCEEDED
            except (MediaProbeError, FileNotFoundError) as exc:
                probe_state = PreparationState.FAILED
                thumbnail_state = PreparationState.SKIPPED
                error_code = "media_probe_failed"
                error_message = str(exc)

            if probe_state is PreparationState.SUCCEEDED:
                if asset.media_type in {MediaType.VIDEO, MediaType.IMAGE}:
                    try:
                        generate_thumbnail(
                            self.library,
                            asset,
                            result.blob_path,
                            ffmpeg_path=self.ffmpeg_path,
                        )
                        thumbnail_state = PreparationState.SUCCEEDED
                    except ThumbnailError as exc:
                        thumbnail_state = PreparationState.FAILED
                        error_code = "thumbnail_failed"
                        error_message = str(exc)
                else:
                    thumbnail_state = PreparationState.SKIPPED

            state = (
                IntakeState.PREPARED
                if error_code is None
                else IntakeState.PARTIAL
            )
            return self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": state,
                    "probe_state": probe_state,
                    "thumbnail_state": thumbnail_state,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        except BaseException as exc:
            current = self.repository.get_intake(intake.intake_id)
            if current is not None and current.state is IntakeState.RECEIVING:
                try:
                    self.repository.transition_intake(
                        intake.intake_id,
                        expected_state=IntakeState.RECEIVING,
                        update={
                            "state": IntakeState.FAILED,
                            "size_bytes": size_bytes,
                            "probe_state": PreparationState.SKIPPED,
                            "thumbnail_state": PreparationState.SKIPPED,
                            "error_code": type(exc).__name__,
                            "error_message": str(exc),
                        },
                    )
                except Exception:
                    pass
            raise
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if staged is not None:
                staged.unlink(missing_ok=True)

    def capture_url_note(
        self,
        *,
        source_url: str | None = None,
        note: str | None = None,
        creator_hint: str | None = None,
        content_kind_hint: str | None = None,
    ) -> InboxIntake:
        intake = self.repository.create_intake(
            InboxIntake(
                kind=IntakeKind.URL_NOTE,
                source_url=source_url,
                note=note,
                creator_hint=creator_hint,
                content_kind_hint=content_kind_hint,
                probe_state=PreparationState.SKIPPED,
                thumbnail_state=PreparationState.SKIPPED,
            )
        )
        try:
            metadata: dict[str, object] = {"inbox_intake_id": intake.intake_id}
            if source_url is not None:
                metadata["source_url"] = source_url
            if note is not None:
                metadata["note"] = note
            if creator_hint is not None:
                metadata["creator_hint"] = creator_hint
            project = Project(
                content_kind=content_kind_hint or "unclassified",
                state=ProjectState.INBOX,
                metadata=metadata,
            )
            self.library.save_project(project)
            return self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.PREPARED,
                    "project_id": project.project_id,
                },
            )
        except BaseException as exc:
            try:
                self.repository.transition_intake(
                    intake.intake_id,
                    expected_state=IntakeState.RECEIVING,
                    update={
                        "state": IntakeState.FAILED,
                        "error_code": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            except Exception:
                pass
            raise

    def thumbnail_path(self, asset_id: str) -> Path | None:
        asset = self.library.database.get_asset(asset_id)
        if asset is None:
            return None
        slot = self.library.database.get_derivative_slot(asset_id, "thumbnail.default")
        if slot is None or slot.storage_key is None:
            return None
        expected_key = thumbnail_storage_key(asset)
        if slot.storage_key != expected_key:
            raise InboxError("thumbnail derivative key is not canonical")
        metadata = slot.metadata
        if metadata.get("source_sha256") != asset.sha256:
            raise InboxError("thumbnail source receipt does not match asset")
        expected_digest = metadata.get("sha256")
        if not isinstance(expected_digest, str) or len(expected_digest) != 64:
            raise InboxError("thumbnail receipt has no valid output digest")
        candidate = (self.library.paths.root / expected_key).resolve()
        root = self.library.paths.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise InboxError("thumbnail storage key escapes runtime root")
        if not candidate.is_file():
            return None
        if sha256_file(candidate) != expected_digest:
            raise InboxError("thumbnail bytes do not match derivative receipt")
        return candidate
