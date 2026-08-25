"""Durable Inbox application service used by API, future PWA, and future CLI."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import BinaryIO

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    Project,
    ProjectState,
    SourceRecord,
    new_entity_id,
)
from content_forge.render.ffmpeg import MediaProbeError, probe_media
from content_forge.storage import LocalLibrary, sha256_file
from content_forge.storage.paths import fsync_directory_chain

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


def _project_id_for_intake(intake: InboxIntake) -> str:
    """Derive one canonical project identity from the intake UUID payload."""

    return f"cf_project_{intake.intake_id.rsplit('_', 1)[1]}"


def _fsync_directory(path: Path) -> None:
    """Persist a newly-created directory entry before publishing its receipt.

    POSIX requires syncing the containing directory in addition to the file itself if a
    power-loss boundary is meant to guarantee that a new filename survives. Windows does
    not expose a portable Python directory-fsync equivalent, so the file handle flush is
    the strongest portable primitive used there.
    """

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _best_effort_unlink(path: Path) -> None:
    """Remove obsolete staging without overturning an accepted/terminal operation."""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Once another verified representation is authoritative, staging is garbage
        # collection. EACCES/EIO here must not turn a completed intake into an HTTP 500.
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

    def _staging_candidates(self, intake: InboxIntake) -> tuple[Path, ...]:
        prefix = f"http-{intake.intake_id}-"
        return tuple(
            sorted(
                path
                for path in self.library.paths.incoming.iterdir()
                if path.is_file() and path.name.startswith(prefix)
            )
        )

    def _verified_staging_candidate(self, intake: InboxIntake) -> Path | None:
        """Return the exact frozen staging file if it survived process interruption."""

        candidates = self._staging_candidates(intake)
        if not candidates:
            return None
        if len(candidates) != 1:
            raise InboxError("multiple staging files claim one Inbox intake")
        if intake.content_sha256 is None or intake.size_bytes is None:
            return None
        candidate = candidates[0]
        if candidate.stat().st_size != intake.size_bytes:
            raise InboxError("staging byte count disagrees with frozen Inbox receipt")
        if sha256_file(candidate) != intake.content_sha256:
            raise InboxError("staging digest disagrees with frozen Inbox receipt")
        return candidate

    def _discard_staging_candidates(self, intake: InboxIntake) -> None:
        try:
            candidates = self._staging_candidates(intake)
        except OSError:
            return
        for path in candidates:
            _best_effort_unlink(path)

    def _source_record_for_intake(self, intake: InboxIntake):
        if intake.source_id is None:
            return None
        record = self.library.database.get_source(intake.source_id)
        if record is None or record.asset_id != intake.asset_id:
            raise InboxError("Inbox provenance linkage is missing or inconsistent")
        return record

    def _ensure_source_record(
        self,
        intake: InboxIntake,
        asset: Asset,
    ) -> tuple[InboxIntake, SourceRecord]:
        """Create/recover provenance under an intake-reserved stable source ID."""

        if intake.source_id is None:
            intake = self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.RECEIVING,
                    "source_id": new_entity_id(EntityKind.SOURCE),
                },
            )
        assert intake.source_id is not None

        existing = self.library.database.get_source(intake.source_id)
        if existing is not None:
            if existing.asset_id != asset.asset_id:
                raise InboxError("Inbox source ID belongs to a different asset")
            return intake, existing

        record = SourceRecord(
            source_id=intake.source_id,
            asset_id=asset.asset_id,
            source_url=intake.source_url,
            creator_name=intake.creator_hint,
            original_title=intake.original_name,
            collected_at=intake.created_at,
            notes=intake.note,
        )
        self.library.database.add_source(record)
        return intake, record

    def _ensure_asset_bytes(self, intake: InboxIntake, asset: Asset) -> Asset:
        """Verify cataloged bytes or republish an authenticated surviving staging copy.

        New AssetStore publications make their canonical directory entry durable before a
        new catalog row can commit. This fallback also repairs receipts produced by older
        interrupted PR8 snapshots where the row survived but the unsynced rename did not.
        Only the exact frozen intake bytes may repair a missing blob; a present-but-corrupt
        canonical blob remains an integrity contradiction and is never overwritten here.
        """

        try:
            verified = self.library.assets.verify(asset)
        except FileNotFoundError as exc:
            staged = self._verified_staging_candidate(intake)
            if staged is None:
                raise InboxError("cataloged Inbox asset blob is missing") from exc
            result = self.library.assets.ingest_file(
                staged,
                source=None,
                media_type=MediaType.OTHER,
                mime_type="application/octet-stream",
            )
            repaired = result.asset
            if repaired.asset_id != asset.asset_id:
                raise InboxError("staging repair resolved to a different asset")
            if repaired.sha256 != asset.sha256 or repaired.size_bytes != asset.size_bytes:
                raise InboxError("staging repair disagrees with cataloged asset identity")
            asset = repaired
            try:
                verified = self.library.assets.verify(asset)
            except FileNotFoundError as second_exc:
                raise InboxError("repaired Inbox asset blob is still missing") from second_exc

        if not verified:
            raise InboxError("cataloged Inbox asset failed byte verification")
        return asset

    def _recover_asset_for_intake(
        self,
        intake: InboxIntake,
    ) -> tuple[InboxIntake, Asset | None]:
        """Recover the asset side of an accepted receiving file intake.

        `content_sha256 + size_bytes` is the byte-acceptance receipt. Once it exists,
        recovery can resume from the verified staging file, canonical blob, asset row,
        or already-linked asset without accepting bytes that disagree with that receipt.
        """

        if intake.asset_id is not None:
            asset = self.library.database.get_asset(intake.asset_id)
            if asset is None:
                raise InboxError("accepted Inbox asset is missing")
            if intake.content_sha256 is not None and asset.sha256 != intake.content_sha256:
                raise InboxError("Inbox content digest disagrees with accepted asset")
            if intake.size_bytes is not None and asset.size_bytes != intake.size_bytes:
                raise InboxError("Inbox byte count disagrees with accepted asset")
            asset = self._ensure_asset_bytes(intake, asset)
            self._discard_staging_candidates(intake)
            return intake, asset

        if intake.content_sha256 is None or intake.size_bytes is None:
            return intake, None

        asset = self.library.database.get_asset_by_sha256(intake.content_sha256)
        if asset is None:
            blob_path = self.library.paths.blob_path_for_sha256(intake.content_sha256)
            if blob_path.is_file():
                if blob_path.stat().st_size != intake.size_bytes:
                    raise InboxError("recovery blob size disagrees with Inbox receipt")
                if sha256_file(blob_path) != intake.content_sha256:
                    raise InboxError("recovery blob digest disagrees with Inbox receipt")
                # A canonical pathname can survive a process interruption even when the
                # AssetStore directory-fsync step itself failed. Re-establish that
                # durability barrier before the first catalog row is allowed to commit.
                # If this raises an operational OSError, reconciliation leaves the FULL-
                # accepted receipt/staging resumable and retries on a later startup.
                fsync_directory_chain(blob_path.parent, stop_at=self.library.paths.root)
                asset = self.library.database.put_asset(
                    Asset(
                        sha256=intake.content_sha256,
                        media_type=MediaType.OTHER,
                        mime_type="application/octet-stream",
                        size_bytes=intake.size_bytes,
                        storage_key=self.library.paths.storage_key_for_sha256(
                            intake.content_sha256
                        ),
                    )
                )
            else:
                staged = self._verified_staging_candidate(intake)
                if staged is None:
                    return intake, None
                result = self.library.assets.ingest_file(
                    staged,
                    source=None,
                    media_type=MediaType.OTHER,
                    mime_type="application/octet-stream",
                )
                asset = result.asset

        if asset.sha256 != intake.content_sha256 or asset.size_bytes != intake.size_bytes:
            raise InboxError("recovered asset metadata disagrees with Inbox receipt")
        asset = self._ensure_asset_bytes(intake, asset)

        # From this point the catalog/canonical blob is authoritative, so an old staging
        # copy is no longer needed even if receipt linkage is interrupted again.
        self._discard_staging_candidates(intake)
        intake = self.repository.transition_intake(
            intake.intake_id,
            expected_state=IntakeState.RECEIVING,
            update={
                "state": IntakeState.RECEIVING,
                "asset_id": asset.asset_id,
            },
        )
        return intake, asset

    def _build_file_project(self, intake: InboxIntake) -> Project:
        if intake.asset_id is None:
            raise InboxError("file intake has no accepted asset")
        source_record = self._source_record_for_intake(intake)
        ref = AssetRef(
            asset_id=intake.asset_id,
            source_id=None if source_record is None else source_record.source_id,
        )
        return Project(
            project_id=_project_id_for_intake(intake),
            content_kind=intake.content_kind_hint or "unclassified",
            state=ProjectState.INBOX,
            source_refs=(ref,),
            source_records=(() if source_record is None else (source_record,)),
            metadata={
                "inbox_intake_id": intake.intake_id,
                "original_filename": intake.original_name or "upload.bin",
            },
            created_at=intake.created_at,
            updated_at=intake.created_at,
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
            project_id=_project_id_for_intake(intake),
            content_kind=intake.content_kind_hint or "unclassified",
            state=ProjectState.INBOX,
            metadata=metadata,
            created_at=intake.created_at,
            updated_at=intake.created_at,
        )

    def _ensure_project(self, intake: InboxIntake) -> tuple[InboxIntake, Project]:
        project = None
        if intake.project_id is not None:
            project = self.library.load_project(intake.project_id)

        canonical_id = _project_id_for_intake(intake)
        if project is None:
            canonical = self.library.load_project(canonical_id)
            if canonical is not None:
                if canonical.metadata.get("inbox_intake_id") != intake.intake_id:
                    raise InboxError("canonical Inbox project ID is already claimed")
                project = canonical

        if project is None:
            # Backward compatibility for receipts created by pre-hardening PR8 snapshots
            # whose project ID was randomly allocated before deterministic recovery.
            project = self.repository.find_project_for_intake(intake.intake_id)
        if project is None:
            project = (
                self._build_file_project(intake)
                if intake.kind is IntakeKind.FILE
                else self._build_url_project(intake)
            )
            self.library.save_project(project)
        if project.metadata.get("inbox_intake_id") != intake.intake_id:
            raise InboxError("Inbox project linkage is inconsistent")
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
                source_id=new_entity_id(EntityKind.SOURCE),
            )
        )
        descriptor: int | None = None
        staged: Path | None = None
        retain_staging = False
        size_bytes = 0
        digest = hashlib.sha256()
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.library.paths.incoming,
                prefix=f"http-{intake.intake_id}-",
                # Staging names are opaque internal recovery keys. The user filename is
                # preserved in the intake/provenance record and must never control a
                # filesystem component (length, encoding, or platform-invalid chars).
                suffix=".upload",
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
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            # File fsync alone does not make a newly-created filename durable on POSIX.
            # Persist both the staged filename and the complete directory chain through
            # the established runtime root before publishing the FULL byte receipt. This
            # matters on the first upload where `.incoming` and its parents may themselves
            # be newly-created directory entries.
            _fsync_directory(staged.parent)
            fsync_directory_chain(staged.parent, stop_at=self.library.paths.root)
            content_sha256 = digest.hexdigest()
            # This is the byte-acceptance linearization point. Before this durable receipt
            # exists, a crash may safely fail the intake. After it exists, the verified
            # staging file/canonical blob/catalog row are all resumable representations.
            # This specific SQLite commit uses synchronous=FULL so returning from the
            # transition means the WAL acceptance receipt has reached stable storage.
            intake = self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.RECEIVING,
                    "size_bytes": size_bytes,
                    "content_sha256": content_sha256,
                },
                durable=True,
            )

            # Client MIME/filename are provenance hints only. Shared immutable Asset
            # classification starts neutral and is promoted only by authoritative probe.
            # Provenance is deliberately attached after the asset receipt checkpoint so
            # every post-acceptance cross-store crash window can be reconstructed.
            result = self.library.assets.ingest_file(
                staged,
                source=None,
                media_type=MediaType.OTHER,
                mime_type="application/octet-stream",
            )
            asset = result.asset
            if asset.sha256 != content_sha256 or asset.size_bytes != size_bytes:
                raise InboxError("AssetStore result disagrees with frozen upload receipt")

            intake = self.repository.transition_intake(
                intake.intake_id,
                expected_state=IntakeState.RECEIVING,
                update={
                    "state": IntakeState.RECEIVING,
                    "asset_id": asset.asset_id,
                },
            )
            intake, _source_record = self._ensure_source_record(intake, asset)
            intake, _project = self._ensure_project(intake)
            return self._prepare_receiving_file(intake, asset)
        except BaseException as exc:
            # Failure handling must not depend on being able to read the receipt again.
            # Under the same disk/SQLite pressure that caused the primary failure,
            # `get_intake()` may itself fail. In that case preserve any staging file
            # conservatively and re-raise the original application failure; exclusive
            # startup reconciliation can later decide from the durable receipt whether
            # those bytes were accepted or are merely pre-acceptance garbage.
            lookup_failed = False
            try:
                current = self.repository.get_intake(intake.intake_id)
            except Exception:
                retain_staging = staged is not None
                lookup_failed = True
                current = None
            except BaseException:
                retain_staging = staged is not None
                raise

            if not lookup_failed and current is not None and current.state is IntakeState.RECEIVING:
                accepted = current.content_sha256 is not None and current.size_bytes is not None
                if not isinstance(exc, Exception):
                    # Shutdown/control-flow signals are not application failures. Once the
                    # FULL byte receipt exists, retain the authenticated staging authority
                    # until a canonical Asset receipt exists, then let exclusive startup
                    # reconciliation resume from whichever durable checkpoint survived.
                    retain_staging = accepted and current.asset_id is None
                else:
                    retryable_operational = isinstance(
                        exc,
                        (OSError, sqlite3.OperationalError),
                    )
                    project_checkpointed = current.project_id is not None
                    if accepted and (retryable_operational or not project_checkpointed):
                        # Accepted bytes with incomplete handoff or a transient storage
                        # failure are resumable, not terminal. Operational failures remain
                        # retryable even after the project checkpoint, matching startup
                        # reconciliation. If no Asset is catalogued yet, authenticated
                        # staging remains the recovery authority.
                        retain_staging = current.asset_id is None
                        try:
                            self.repository.transition_intake(
                                current.intake_id,
                                expected_state=IntakeState.RECEIVING,
                                update={
                                    "state": IntakeState.RECEIVING,
                                    "error_code": "post_acceptance_retryable",
                                    "error_message": "accepted upload awaits recovery",
                                },
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            self.repository.transition_intake(
                                current.intake_id,
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
            if staged is not None and not retain_staging:
                _best_effort_unlink(staged)

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
        except Exception as exc:
            retryable_operational = isinstance(
                exc,
                (OSError, sqlite3.OperationalError),
            )
            if retryable_operational:
                # URL/note capture has no accepted byte authority to protect, but its
                # durable receipt plus deterministic project checkpoints are recoverable.
                # Once that RECEIVING receipt can be read back, return it to the caller
                # instead of presenting the request as unaccepted and encouraging a retry
                # that would allocate a second intake/project. Diagnostic persistence is
                # best effort under the same storage pressure; the receipt identity/state
                # are the authority and exclusive reconciliation completes it later.
                current = self.repository.get_intake(intake.intake_id)
                if current is not None and current.state is IntakeState.RECEIVING:
                    try:
                        return self.repository.transition_intake(
                            current.intake_id,
                            expected_state=IntakeState.RECEIVING,
                            update={
                                "state": IntakeState.RECEIVING,
                                "error_code": "capture_retryable",
                                "error_message": "URL/note capture awaits recovery",
                            },
                        )
                    except Exception:
                        return current
                raise
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

        A file is accepted only once exact digest+size are durable. From that point,
        startup can recover from durable staging, canonical-blob publication, asset
        catalog, provenance, deterministic project, and receipt-link interruptions.
        Control-flow exceptions such as KeyboardInterrupt/SystemExit are never recovery
        failures: they propagate without mutating the receipt or deleting staging.
        """

        recovered: list[InboxIntake] = []
        for original in self.repository.list_intakes_in_state(IntakeState.RECEIVING):
            try:
                intake = original
                if intake.kind is IntakeKind.FILE:
                    intake, asset = self._recover_asset_for_intake(intake)
                    if asset is None:
                        failed = self.repository.transition_intake(
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
                        self._discard_staging_candidates(intake)
                        recovered.append(failed)
                        continue
                    intake, _source_record = self._ensure_source_record(intake, asset)
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
            except Exception as exc:
                current = self.repository.get_intake(original.intake_id)
                if current is not None and current.state is IntakeState.RECEIVING:
                    accepted_file = (
                        current.kind is IntakeKind.FILE
                        and current.content_sha256 is not None
                        and current.size_bytes is not None
                    )
                    retryable_storage_error = isinstance(
                        exc,
                        (OSError, sqlite3.OperationalError),
                    )
                    retryable_receipt = retryable_storage_error and (
                        accepted_file or current.kind is IntakeKind.URL_NOTE
                    )
                    if retryable_receipt:
                        # FULL-accepted files keep operational storage failures retryable
                        # so their authenticated byte authority is not destroyed. URL/note
                        # records have no byte acceptance boundary, but their deterministic
                        # project linkage is likewise reconstructible, so storage pressure
                        # must leave them RECEIVING for a later exclusive startup.
                        recovered.append(current)
                        continue
                    try:
                        failed = self.repository.transition_intake(
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
                        self._discard_staging_candidates(current)
                        recovered.append(failed)
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