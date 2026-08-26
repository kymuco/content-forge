"""SQLite persistence for application-layer Inbox and local-auth records."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from content_forge.core import Asset, Project, dump_json, load_json
from content_forge.storage import (
    LibraryDatabase,
    StorageConflictError,
    StorageError,
    StorageSchemaError,
)

from .idempotency import (
    IdempotencyConflict,
    IdempotencyReplay,
    current_idempotency_key,
    intake_id_for_key,
    same_intake_request,
)
from .models import InboxIntake, IntakeKind, IntakeState, PreparationState

APPLICATION_SCHEMA_VERSION = 1
APPLICATION_SCHEMA_COMPONENT = "application"
# Before a file has a durable size+SHA receipt, no bytes have crossed the acceptance
# boundary and retrying the same idempotency identity is safe. Keep only deterministic
# input failures terminal; operational subclasses (PermissionError, FileNotFoundError,
# platform-specific OSError subclasses, SQLite failures, etc.) therefore do not need to be
# reconstructed later from fragile exception class-name strings.
_PERMANENT_PREACCEPTANCE_FILE_FAILURES = frozenset({"UploadTooLargeError"})


class ApplicationRepository:
    """Own PR8 application tables without leaking SQL into HTTP handlers."""

    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

    @contextmanager
    def _transaction(self, *, durable: bool = False) -> Iterator[sqlite3.Connection]:
        """Open an application transaction, optionally making its commit power-loss durable.

        LibraryDatabase normally uses WAL + synchronous=NORMAL for routine metadata
        throughput. PR8 has one stronger linearization point: the exact size+SHA upload
        acceptance receipt. That transaction uses synchronous=FULL before BEGIN so the
        WAL commit is synchronized before returning. Later checkpoints remain NORMAL
        because they are reconstructible from the durable acceptance receipt and verified
        byte representations.
        """

        if not durable:
            with self.database.transaction() as connection:
                yield connection
            return

        with self.database.connection() as connection:
            connection.execute("PRAGMA synchronous = FULL")
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> "ApplicationRepository":
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM application_schema WHERE component = ?",
                (APPLICATION_SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > APPLICATION_SCHEMA_VERSION:
                raise StorageSchemaError(
                    f"application schema {version} is newer than supported "
                    f"{APPLICATION_SCHEMA_VERSION}"
                )
            if version not in {0, APPLICATION_SCHEMA_VERSION}:
                raise StorageSchemaError(
                    f"unsupported application schema migration: {version} -> "
                    f"{APPLICATION_SCHEMA_VERSION}"
                )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_intakes (
                    intake_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbox_intakes_state_created
                ON inbox_intakes(state, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pairing_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    salt TEXT NOT NULL,
                    code_digest TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_id TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL UNIQUE,
                    label TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_token
                ON auth_sessions(token_digest)
                """
            )
            if version == 0:
                connection.execute(
                    """
                    INSERT INTO application_schema(component, version)
                    VALUES (?, ?)
                    """,
                    (APPLICATION_SCHEMA_COMPONENT, APPLICATION_SCHEMA_VERSION),
                )
        return self

    def create_intake(self, intake: InboxIntake) -> InboxIntake:
        """Create one intake, or resume/replay the identity bound to a retry key.

        The raw client UUID is never persisted as authority. While an authenticated API
        call holds an idempotency scope, the UUID deterministically selects the intake ID.
        That durable primary key is therefore committed in the same SQLite transaction as
        the initial receipt: losing the HTTP response cannot create a second lineage.
        """

        idempotency_key = current_idempotency_key()
        if idempotency_key is not None:
            intake = intake.validated_copy(
                update={"intake_id": intake_id_for_key(idempotency_key)}
            )

        with self.database.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO inbox_intakes(
                        intake_id, kind, state, manifest_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intake.intake_id,
                        intake.kind.value,
                        intake.state.value,
                        dump_json(intake),
                        intake.created_at.isoformat(),
                        intake.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if idempotency_key is None:
                    raise StorageConflictError(
                        f"intake ID already exists: {intake.intake_id}"
                    ) from exc

                row = connection.execute(
                    "SELECT manifest_json FROM inbox_intakes WHERE intake_id = ?",
                    (intake.intake_id,),
                ).fetchone()
                if row is None:
                    raise StorageConflictError(
                        f"intake ID already exists: {intake.intake_id}"
                    ) from exc
                current_json = row["manifest_json"]
                existing = load_json(InboxIntake, current_json)
                if not same_intake_request(existing, intake):
                    raise IdempotencyConflict(
                        "Idempotency-Key was reused for different capture metadata"
                    ) from exc

                accepted_file = (
                    existing.kind is IntakeKind.FILE
                    and existing.content_sha256 is not None
                    and existing.size_bytes is not None
                )
                if existing.state is IntakeState.RECEIVING and (
                    existing.kind is IntakeKind.URL_NOTE or not accepted_file
                ):
                    # The first attempt did not cross the file byte-acceptance boundary,
                    # or a URL/note operation is still at a reconstructible checkpoint.
                    # Reuse the exact durable identity and let the service resume it.
                    return existing

                retryable_preacceptance_failure = (
                    existing.kind is IntakeKind.FILE
                    and existing.state is IntakeState.FAILED
                    and existing.content_sha256 is None
                    and existing.asset_id is None
                    and existing.project_id is None
                    and existing.error_code not in _PERMANENT_PREACCEPTANCE_FILE_FAILURES
                )
                if retryable_preacceptance_failure:
                    # No exact byte receipt exists, so this durable identity has accepted
                    # no content. Revive it regardless of the concrete operational error
                    # class; terminal input failures are explicitly deny-listed above.
                    # This keeps PermissionError/FileNotFoundError/platform OSError
                    # subclasses retryable without encoding Python's exception hierarchy
                    # into persisted class-name strings.
                    revived = existing.validated_copy(
                        update={
                            "state": IntakeState.RECEIVING,
                            "size_bytes": None,
                            "content_sha256": None,
                            "probe_state": PreparationState.PENDING,
                            "thumbnail_state": PreparationState.PENDING,
                            "error_code": None,
                            "error_message": None,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    )
                    changed = connection.execute(
                        """
                        UPDATE inbox_intakes
                        SET state = ?, manifest_json = ?, updated_at = ?
                        WHERE intake_id = ? AND state = ? AND manifest_json = ?
                        """,
                        (
                            revived.state.value,
                            dump_json(revived),
                            revived.updated_at.isoformat(),
                            revived.intake_id,
                            IntakeState.FAILED.value,
                            current_json,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise StorageConflictError(
                            f"intake {existing.intake_id} could not revive for retry"
                        ) from exc
                    return revived

                # Prepared/partial/permanently-failed records, plus FULL-accepted
                # receiving files, already represent the request identity. The HTTP
                # adapter replays that durable result instead of executing side effects
                # a second time.
                raise IdempotencyReplay(existing) from exc
        return intake

    def get_intake(self, intake_id: str) -> InboxIntake | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM inbox_intakes WHERE intake_id = ?",
                (intake_id,),
            ).fetchone()
        return None if row is None else load_json(InboxIntake, row["manifest_json"])

    def list_intakes(self, *, limit: int = 100) -> tuple[InboxIntake, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT manifest_json FROM inbox_intakes
                ORDER BY created_at DESC, intake_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(load_json(InboxIntake, row["manifest_json"]) for row in rows)

    def list_intakes_in_state(self, state: IntakeState) -> tuple[InboxIntake, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT manifest_json FROM inbox_intakes
                WHERE state = ? ORDER BY created_at, intake_id
                """,
                (state.value,),
            ).fetchall()
        return tuple(load_json(InboxIntake, row["manifest_json"]) for row in rows)

    def find_project_for_intake(self, intake_id: str) -> Project | None:
        """Recover a project committed before its intake receipt linkage."""

        matches: list[Project] = []
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM projects ORDER BY created_at, project_id"
            ).fetchall()
        for row in rows:
            project = load_json(Project, row["manifest_json"])
            if project.metadata.get("inbox_intake_id") == intake_id:
                matches.append(project)
        if len(matches) > 1:
            raise StorageConflictError(
                f"multiple projects claim Inbox intake {intake_id}"
            )
        return None if not matches else matches[0]

    def transition_intake(
        self,
        intake_id: str,
        *,
        expected_state: IntakeState,
        update: dict[str, object],
        durable: bool = False,
    ) -> InboxIntake:
        with self._transaction(durable=durable) as connection:
            row = connection.execute(
                "SELECT manifest_json, state FROM inbox_intakes WHERE intake_id = ?",
                (intake_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown intake: {intake_id}")
            if row["state"] != expected_state.value:
                raise StorageConflictError(
                    f"intake {intake_id} state changed: expected "
                    f"{expected_state.value!r}, found {row['state']!r}"
                )
            current_json = row["manifest_json"]
            current = load_json(InboxIntake, current_json)
            payload = dict(update)
            payload["updated_at"] = datetime.now(timezone.utc)
            updated = current.validated_copy(update=payload)
            changed = connection.execute(
                """
                UPDATE inbox_intakes
                SET kind = ?, state = ?, manifest_json = ?, updated_at = ?
                WHERE intake_id = ? AND state = ? AND manifest_json = ?
                """,
                (
                    updated.kind.value,
                    updated.state.value,
                    dump_json(updated),
                    updated.updated_at.isoformat(),
                    intake_id,
                    expected_state.value,
                    current_json,
                ),
            ).rowcount
            if changed != 1:
                raise StorageConflictError(
                    f"intake {intake_id} could not transition from "
                    f"{expected_state.value!r}"
                )
        return updated

    def enrich_asset(self, enriched: Asset) -> Asset:
        """Persist authoritative media-probe metadata without changing byte identity.

        Asset rows predating PR8 may contain media type/MIME inferred from a filename or
        caller-supplied header. Those fields were never authoritative. A successful
        ffprobe is allowed to repair all media-derived fields on such a deduplicated
        asset, while byte identity and storage identity remain immutable. Re-probing the
        same bytes is therefore an idempotent authoritative refresh rather than a client
        reclassification path.
        """

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM assets WHERE asset_id = ?",
                (enriched.asset_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown asset: {enriched.asset_id}")
            current_json = row["manifest_json"]
            current = load_json(Asset, current_json)
            immutable_fields = (
                "asset_id",
                "sha256",
                "size_bytes",
                "storage_key",
                "created_at",
            )
            for field in immutable_fields:
                if getattr(current, field) != getattr(enriched, field):
                    raise StorageConflictError(
                        f"asset enrichment attempted to change immutable {field}"
                    )

            enriched_json = dump_json(enriched)
            if enriched_json == current_json:
                return current

            changed = connection.execute(
                """
                UPDATE assets SET manifest_json = ?
                WHERE asset_id = ? AND manifest_json = ?
                """,
                (enriched_json, enriched.asset_id, current_json),
            ).rowcount
            if changed != 1:
                raise StorageConflictError(
                    f"asset changed concurrently during enrichment: {enriched.asset_id}"
                )
        return enriched
