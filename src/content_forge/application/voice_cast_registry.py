"""SQLite-backed PR21 runtime-wide voice-cast registry."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from content_forge.core import MediaType, dump_json, load_json
from content_forge.storage import LocalLibrary, StorageSchemaError

from .voice_cast_models import (
    MAX_CAST_ENTRIES,
    VoiceCastConflictError,
    VoiceCastDefinition,
    VoiceCastNotFoundError,
    VoiceCastRevision,
    VoiceCastValidationError,
    cast_definition_digest,
)

_SCHEMA_COMPONENT = "voice_cast"
_SCHEMA_VERSION = 1


class _VoiceCastRepository:
    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.database = library.database

    def initialize(self) -> "_VoiceCastRepository":
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
                (_SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > _SCHEMA_VERSION:
                raise StorageSchemaError(
                    f"voice cast schema {version} is newer than supported {_SCHEMA_VERSION}"
                )
            if version not in {0, _SCHEMA_VERSION}:
                raise StorageSchemaError(
                    f"unsupported voice cast schema migration: {version} -> {_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_cast_revisions (
                    cast_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    reference_asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (cast_id, revision)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voice_cast_revision_created
                ON voice_cast_revisions(cast_id, revision DESC, created_at DESC)
                """
            )
            if version == 0:
                connection.execute(
                    "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                    (_SCHEMA_COMPONENT, _SCHEMA_VERSION),
                )
        return self

    @staticmethod
    def _decode(raw: str) -> VoiceCastRevision:
        try:
            return load_json(VoiceCastRevision, raw)
        except Exception as exc:
            raise VoiceCastValidationError("stored voice cast revision is malformed") from exc

    def get(self, cast_id: str, revision: int | None = None) -> VoiceCastRevision | None:
        with self.database.connection() as connection:
            if revision is None:
                row = connection.execute(
                    """
                    SELECT manifest_json FROM voice_cast_revisions
                    WHERE cast_id = ? ORDER BY revision DESC LIMIT 1
                    """,
                    (cast_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT manifest_json FROM voice_cast_revisions
                    WHERE cast_id = ? AND revision = ?
                    """,
                    (cast_id, revision),
                ).fetchone()
        return None if row is None else self._decode(str(row["manifest_json"]))

    def list_latest(self, *, limit: int) -> tuple[VoiceCastRevision, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT current.manifest_json
                FROM voice_cast_revisions AS current
                WHERE current.revision = (
                    SELECT MAX(candidate.revision)
                    FROM voice_cast_revisions AS candidate
                    WHERE candidate.cast_id = current.cast_id
                )
                ORDER BY current.cast_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._decode(str(row["manifest_json"])) for row in rows)

    def put(self, definition: VoiceCastDefinition) -> VoiceCastRevision:
        digest = cast_definition_digest(
            cast_id=definition.cast_id,
            display_name=definition.display_name,
            settings=definition.settings,
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT manifest_json FROM voice_cast_revisions
                WHERE cast_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (definition.cast_id,),
            ).fetchone()
            current = None if row is None else self._decode(str(row["manifest_json"]))
            if current is not None and current.definition_sha256 == digest:
                return current
            next_revision = 1 if current is None else current.revision + 1
            revision = VoiceCastRevision(
                cast_id=definition.cast_id,
                revision=next_revision,
                display_name=definition.display_name,
                settings=definition.settings,
                definition_sha256=digest,
                created_at=datetime.now(timezone.utc),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO voice_cast_revisions(
                        cast_id, revision, reference_asset_id, manifest_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        revision.cast_id,
                        revision.revision,
                        revision.settings.reference_asset_id,
                        dump_json(revision),
                        revision.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise VoiceCastConflictError("voice cast revision could not be committed") from exc
        return revision


class VoiceCastRegistry:
    """Public registry that also verifies any retained reference-audio asset."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.repository = _VoiceCastRepository(library).initialize()

    def _validate_reference(self, settings) -> None:
        reference_asset_id = settings.reference_asset_id
        if reference_asset_id is None:
            return
        asset = self.library.database.get_asset(reference_asset_id)
        if asset is None:
            raise VoiceCastNotFoundError(
                f"unknown voice cast reference asset: {reference_asset_id}"
            )
        if asset.media_type is not MediaType.AUDIO:
            raise VoiceCastValidationError("voice cast reference asset must be audio")
        try:
            verified = self.library.assets.verify(asset)
        except (FileNotFoundError, OSError) as exc:
            raise VoiceCastConflictError("voice cast reference audio bytes are unavailable") from exc
        if not verified:
            raise VoiceCastConflictError("voice cast reference audio failed content verification")

    def put(self, definition: VoiceCastDefinition) -> VoiceCastRevision:
        self._validate_reference(definition.settings)
        return self.repository.put(definition)

    def get(self, cast_id: str, revision: int | None = None) -> VoiceCastRevision:
        item = self.repository.get(cast_id, revision)
        if item is None:
            suffix = "latest" if revision is None else str(revision)
            raise VoiceCastNotFoundError(f"unknown voice cast revision: {cast_id}@{suffix}")
        self._validate_reference(item.settings)
        return item

    def list_latest(self, *, limit: int = MAX_CAST_ENTRIES) -> tuple[VoiceCastRevision, ...]:
        if limit < 1 or limit > MAX_CAST_ENTRIES:
            raise VoiceCastValidationError("voice cast list limit is outside allowed range")
        items = self.repository.list_latest(limit=limit)
        for item in items:
            self._validate_reference(item.settings)
        return items


__all__ = ["VoiceCastRegistry"]
