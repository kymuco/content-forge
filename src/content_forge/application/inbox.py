"""Durable Inbox application service used by API, future PWA, and future CLI."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from content_forge.core import Asset, AssetRef, MediaType, Project, ProjectState
from content_forge.render.ffmpeg import MediaProbeError, probe_media
from content_forge.storage import LocalLibrary, SourceInput, sha256_file

from .media import (
    ThumbnailError,
    apply_authoritative_probe,
    generate_thumbnail,
    thumbnail_storage_key,
)
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


def _public_failure_message(exc: BaseException) -> str:
    """Return a path-safe durable/API diagnostic.

    Detailed subprocess/storage exceptions can contain absolute runtime paths. Those
    belong in future local-only diagnostic logging, not in Inbox records returned to a
    phone client.
    """

    if isinstance(exc, UploadTooLargeError):
        return str(exc)
    return "operation failed"


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

    def _source_record_for_intake(self, intake: InboxIntake):
        if intake.source_id is None:
            return None
        record = self.library.database.get_source(intake.source_id)
        if record is None or record.asset_id != intake.asset_id:
            raise InboxError("Inbox provenance linkage is missing or inconsistent")
        return record

    def _build_file_project(self, intake: InboxIntake) -> Project:
        if intake.asset_id is None:
            raise InboxError("file intake has no accepted asset")
        source_record = self._source_record_for_intake(intake)
        ref = AssetRef(
            asset_id=intake.asset_id,
            source_id=None if source_record is None else source_record.source_id,
        )
        return Project(
            content_kind=intake.content_kind_hint or "unclassified",
            state=ProjectState.INBOX,
            source_refs=(ref,),
            source_records=(() if source_record is None else (source_record,)),
            metadata={
                "inbox_intake_id": intake.intake_id,
                "original_filename": intake.original_name or "upload.bin",
            },
        )

    def _build_url_project(self, intake: InboxIntake) -> Project:
        metadata: dict[str, object] = {"inbox_intake_id": intake.intake_id}
        if intake.source_url is not None:
            metadata["source_url"] = intake.source_url
        if intake.note is not None:
            metadata["note"] = intake.note
        if intake.creator_hint is not None:
            metadata["creator_hint"] = intake.creator_hint
        return Project(
            content_kind=intake.content_kind_hint or "unclassified",
            state=ProjectState.INBOX,
            metadata=metadata,
        )

    def _ensure_project(self, intake: InboxIntake) -> tuple[InboxIntake, Project]:
        project = None
        if intake.project_id is not None:
            project = self.library.load_project(intake.project_id)
        if project is None:
            project = self.repository.find_project_for_intake(intake.intake_id)
        if project is None:
            project = (
                self._build_file_project(intake)
                if intake.kind is IntakeKind.FILE
                else self._build_url_project(intake)
            )
            self.library.save_project(project)
        if intake.project_id != project.project_id:
            intake = self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.RECEIVING,
                    "project_id": project.project_id,
                },
            )
        return intake, project

    def _prepare_receiving_file(self, intake: InboxIntake, asset: Asset) -> InboxIntake:
        source_path = self.library.assets.resolve(asset)
        probe_state = PreparationState.PENDING
        thumbnail_state = PreparationState.PENDING
        error_code: str | None = None
        error_message: str | None = None

        try:
            probe = probe_media(source_path, ffprobe_path=self.ffprobe_path)
            asset = apply_authoritative_probe(asset, probe)
            self.repository.enrich_asset(asset)
            probe_state = PreparationState.SUCCEEDED
        except (MediaProbeError, FileNotFoundError):
            probe_state = PreparationState.FAILED
            thumbnail_state = PreparationState.SKIPPED
            error_code = "media_probe_failed"
            error_message = "media probe failed"

        if probe_state is PreparationState.SUCCEEDED:
            if asset.media_type in {MediaType.VIDEO, MediaType.IMAGE}:
                try:
                    generate_thumbnail(
                        self.library,
                        asset,
                        source_path,
                        ffmpeg_path=self.ffmpeg_path,
                    )
                    thumbnail_state = PreparationState.SUCCEEDED
                except ThumbnailError:
                    thumbnail_state = PreparationState.FAILED
                    error_code = "thumbnail_failed"
                    error_message = "thumbnail generation failed"
            else:
                thumbnail_state = PreparationState.SKIPPED

        state = IntakeState.PREPARED if error_code is None else IntakeState.PARTIAL
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
            # Client MIME/filename are provenance hints only. Shared immutable Asset
            # classification starts neutral and is promoted only by authoritative probe.
            result = self.library.assets.ingest_file(
                staged,
                source=source,
                media_type=MediaType.OTHER,
                mime_type="application/octet-stream",
            )
            asset = result.asset
            source_id = (
                None if result.source_record is None else result.source_record.source_id
            )

            intake = self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.RECEIVING,
                    "size_bytes": size_bytes,
                    "asset_id": asset.asset_id,
                    "source_id": source_id,
                },
            )
            intake, _project = self._ensure_project(intake)
            return self._prepare_receiving_file(intake, asset)
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
                            "error_message": _public_failure_message(exc),
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
            intake, _project = self._ensure_project(intake)
            return self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={"state": IntakeState.PREPARED},
            )
        except BaseException as exc:
            try:
                self.repository.transition_intake(
                    intake.intake_id,
                    expected_state=IntakeState.RECEIVING,
                    update={
                        "state": IntakeState.FAILED,
                        "error_code": type(exc).__name__,
                        "error_message": _public_failure_message(exc),
                    },
                )
            except Exception:
                pass
            raise

    def reconcile_receiving(self) -> tuple[InboxIntake, ...]:
        """Recover receipts left `receiving` by process/machine interruption.

        Project manifests carry `metadata.inbox_intake_id`, so a project committed in the
        small save-project -> receipt-link crash window is discoverable without creating a
        duplicate. File preparation is resumed from immutable asset bytes; URL/note intake
        only needs project linkage restored.
        """

        recovered: list[InboxIntake] = []
        for original in self.repository.list_intakes_in_state(IntakeState.RECEIVING):
            try:
                intake = original
                if intake.kind is IntakeKind.FILE:
                    if intake.asset_id is None:
                        recovered.append(
                            self.repository.transition_intake(
                                intake.intake_id,
                                expected_state=IntakeState.RECEIVING,
                                update={
                                    "state": IntakeState.FAILED,
                                    "probe_state": PreparationState.SKIPPED,
                                    "thumbnail_state": PreparationState.SKIPPED,
                                    "error_code": "interrupted_before_asset_acceptance",
                                    "error_message": "upload interrupted before asset acceptance",
                                },
                            )
                        )
                        continue
                    asset = self.library.database.get_asset(intake.asset_id)
                    if asset is None:
                        raise InboxError("accepted Inbox asset is missing")
                    intake, _project = self._ensure_project(intake)
                    recovered.append(self._prepare_receiving_file(intake, asset))
                else:
                    intake, _project = self._ensure_project(intake)
                    recovered.append(
                        self.repository.transition_intake(
                            intake.intake_id,
                            expected_state=IntakeState.RECEIVING,
                            update={"state": IntakeState.PREPARED},
                        )
                    )
            except BaseException:
                current = self.repository.get_intake(original.intake_id)
                if current is not None and current.state is IntakeState.RECEIVING:
                    try:
                        recovered.append(
                            self.repository.transition_intake(
                                current.intake_id,
                                expected_state=IntakeState.RECEIVING,
                                update={
                                    "state": IntakeState.FAILED,
                                    "probe_state": PreparationState.SKIPPED,
                                    "thumbnail_state": PreparationState.SKIPPED,
                                    "error_code": "interrupted_recovery_failed",
                                    "error_message": "interrupted Inbox recovery failed",
                                },
                            )
                        )
                    except Exception:
                        pass
        return tuple(recovered)

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
