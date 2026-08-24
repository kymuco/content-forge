"""SQLite persistence for application-layer Inbox and local-auth records."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from content_forge.core import Asset, dump_json, load_json
from content_forge.storage import (
    LibraryDatabase,
    StorageConflictError,
    StorageError,
    StorageSchemaError,
)

from .models import InboxIntake, IntakeState

APPLICATION_SCHEMA_VERSION = 1
APPLICATION_SCHEMA_COMPONENT = "application"


class ApplicationRepository:
    """Own PR8 application tables without leaking SQL into HTTP handlers."""

    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

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
                raise StorageConflictError(
                    f"intake ID already exists: {intake.intake_id}"
                ) from exc
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

    def transition_intake(
        self,
        intake_id: str,
        *,
        expected_state: IntakeState,
        update: dict[str, object],
    ) -> InboxIntake:
        with self.database.transaction() as connection:
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
        """Persist probe metadata while preserving immutable asset byte identity."""

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
                "media_type",
                "mime_type",
                "size_bytes",
                "storage_key",
                "created_at",
            )
            for field in immutable_fields:
                if getattr(current, field) != getattr(enriched, field):
                    raise StorageConflictError(
                        f"asset enrichment attempted to change immutable {field}"
                    )
            changed = connection.execute(
                """
                UPDATE assets SET manifest_json = ?
                WHERE asset_id = ? AND manifest_json = ?
                """,
                (dump_json(enriched), enriched.asset_id, current_json),
            ).rowcount
            if changed != 1:
                raise StorageConflictError(
                    f"asset changed concurrently during enrichment: {enriched.asset_id}"
                )
        return enriched
