"""SQLite metadata catalog for the local Content Forge library."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from content_forge.core import Asset, AssetRef, Project, SourceRecord, dump_json, load_json

from .records import DerivativeSlot, StoredJob

DATABASE_SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    """Base class for local-library persistence failures."""


class StorageSchemaError(StorageError):
    pass


class MissingAssetError(StorageError):
    pass


class MissingProjectError(StorageError):
    pass


class StorageConflictError(StorageError):
    pass


def _json_plain(value: object) -> object:
    """Thaw immutable JSON containers without accepting arbitrary object coercion."""

    if isinstance(value, Mapping):
        return {key: _json_plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_plain(item) for item in value]
    return value


def _strict_json(value: object) -> str:
    return json.dumps(
        _json_plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _project_refs(project: Project) -> tuple[AssetRef, ...]:
    refs: list[AssetRef] = list(project.source_refs)
    for scene in project.scenes:
        if scene.media is not None:
            refs.append(scene.media)
        refs.extend(
            overlay.asset_ref for overlay in scene.overlays if overlay.asset_ref is not None
        )
        refs.extend(
            track.asset_ref for track in scene.audio_tracks if track.asset_ref is not None
        )
    refs.extend(
        overlay.asset_ref for overlay in project.overlays if overlay.asset_ref is not None
    )
    refs.extend(
        track.asset_ref for track in project.audio_tracks if track.asset_ref is not None
    )
    return tuple(refs)


class LibraryDatabase:
    """Small SQLite catalog using one short-lived connection per operation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> "LibraryDatabase":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_SCHEMA_VERSION:
                raise StorageSchemaError(
                    f"database schema {version} is newer than supported "
                    f"{DATABASE_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS assets (
                        asset_id TEXT PRIMARY KEY,
                        sha256 TEXT NOT NULL UNIQUE,
                        manifest_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS sources (
                        source_id TEXT PRIMARY KEY,
                        asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                        manifest_json TEXT NOT NULL,
                        collected_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sources_asset_id
                        ON sources(asset_id);

                    CREATE TABLE IF NOT EXISTS projects (
                        project_id TEXT PRIMARY KEY,
                        content_kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        manifest_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS project_assets (
                        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                        asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
                        source_id TEXT REFERENCES sources(source_id) ON DELETE RESTRICT,
                        role TEXT NOT NULL,
                        PRIMARY KEY (project_id, asset_id, source_id, role)
                    );
                    CREATE INDEX IF NOT EXISTS idx_project_assets_asset_id
                        ON project_assets(asset_id);

                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
                        job_type TEXT NOT NULL,
                        state TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id);
                    CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

                    CREATE TABLE IF NOT EXISTS derivative_slots (
                        asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                        slot TEXT NOT NULL,
                        storage_key TEXT,
                        metadata_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (asset_id, slot)
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
                connection.commit()
        return self

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def put_asset(self, asset: Asset) -> Asset:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assets(asset_id, sha256, manifest_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (asset.asset_id, asset.sha256, dump_json(asset), asset.created_at.isoformat()),
            )
            row = connection.execute(
                "SELECT manifest_json FROM assets WHERE sha256 = ?", (asset.sha256,)
            ).fetchone()
            if row is None:
                raise StorageError("asset insert did not produce a readable row")
            stored = load_json(Asset, row["manifest_json"])
            if stored.sha256 != asset.sha256:
                raise StorageConflictError("asset digest conflict")
            return stored

    def get_asset(self, asset_id: str) -> Asset | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        return None if row is None else load_json(Asset, row["manifest_json"])

    def get_asset_by_sha256(self, digest: str) -> Asset | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM assets WHERE sha256 = ?", (digest,)
            ).fetchone()
        return None if row is None else load_json(Asset, row["manifest_json"])

    def add_source(self, record: SourceRecord) -> SourceRecord:
        with self.transaction() as connection:
            self._put_source(connection, record)
        return record

    def _put_source(self, connection: sqlite3.Connection, record: SourceRecord) -> None:
        asset_exists = connection.execute(
            "SELECT 1 FROM assets WHERE asset_id = ?", (record.asset_id,)
        ).fetchone()
        if asset_exists is None:
            raise MissingAssetError(f"unknown asset: {record.asset_id}")

        existing = connection.execute(
            "SELECT manifest_json FROM sources WHERE source_id = ?", (record.source_id,)
        ).fetchone()
        if existing is not None:
            stored = load_json(SourceRecord, existing["manifest_json"])
            if stored != record:
                raise StorageConflictError(
                    f"source ID already exists with different metadata: {record.source_id}"
                )
            return

        connection.execute(
            """
            INSERT INTO sources(source_id, asset_id, manifest_json, collected_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                record.source_id,
                record.asset_id,
                dump_json(record),
                record.collected_at.isoformat(),
            ),
        )

    def get_source(self, source_id: str) -> SourceRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        return None if row is None else load_json(SourceRecord, row["manifest_json"])

    def list_sources(self, asset_id: str) -> tuple[SourceRecord, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT manifest_json FROM sources
                WHERE asset_id = ?
                ORDER BY collected_at, source_id
                """,
                (asset_id,),
            ).fetchall()
        return tuple(load_json(SourceRecord, row["manifest_json"]) for row in rows)

    def save_project(self, project: Project) -> Project:
        refs = _project_refs(project)
        with self.transaction() as connection:
            for record in project.source_records:
                self._put_source(connection, record)

            for ref in refs:
                if connection.execute(
                    "SELECT 1 FROM assets WHERE asset_id = ?", (ref.asset_id,)
                ).fetchone() is None:
                    raise MissingAssetError(
                        f"project references unknown asset: {ref.asset_id}"
                    )
                if ref.source_id is not None:
                    row = connection.execute(
                        "SELECT asset_id FROM sources WHERE source_id = ?", (ref.source_id,)
                    ).fetchone()
                    if row is None or row["asset_id"] != ref.asset_id:
                        raise StorageConflictError(
                            "project provenance reference does not match stored asset"
                        )

            connection.execute(
                """
                INSERT INTO projects(
                    project_id, content_kind, state, manifest_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    content_kind = excluded.content_kind,
                    state = excluded.state,
                    manifest_json = excluded.manifest_json,
                    updated_at = excluded.updated_at
                """,
                (
                    project.project_id,
                    project.content_kind,
                    project.state.value,
                    dump_json(project),
                    project.created_at.isoformat(),
                    project.updated_at.isoformat(),
                ),
            )
            connection.execute(
                "DELETE FROM project_assets WHERE project_id = ?", (project.project_id,)
            )
            unique_refs = {(ref.asset_id, ref.source_id, str(ref.role)) for ref in refs}
            connection.executemany(
                """
                INSERT INTO project_assets(project_id, asset_id, source_id, role)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (project.project_id, asset_id, source_id, role)
                    for asset_id, source_id, role in sorted(
                        unique_refs, key=lambda item: (item[0], item[1] or "", item[2])
                    )
                ],
            )
        return project

    def load_project(self, project_id: str) -> Project | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return None if row is None else load_json(Project, row["manifest_json"])

    def project_ids_for_asset(self, asset_id: str) -> tuple[str, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT project_id FROM project_assets
                WHERE asset_id = ? ORDER BY project_id
                """,
                (asset_id,),
            ).fetchall()
        return tuple(row["project_id"] for row in rows)

    def put_derivative_slot(self, slot: DerivativeSlot) -> DerivativeSlot:
        if self.get_asset(slot.asset_id) is None:
            raise MissingAssetError(f"unknown asset: {slot.asset_id}")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO derivative_slots(
                    asset_id, slot, storage_key, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(asset_id, slot) DO UPDATE SET
                    storage_key = excluded.storage_key,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    slot.asset_id,
                    slot.slot,
                    slot.storage_key,
                    _strict_json(slot.metadata),
                    slot.updated_at.isoformat(),
                ),
            )
        return slot

    def get_derivative_slot(self, asset_id: str, slot: str) -> DerivativeSlot | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT storage_key, metadata_json, updated_at
                FROM derivative_slots WHERE asset_id = ? AND slot = ?
                """,
                (asset_id, slot),
            ).fetchone()
        if row is None:
            return None
        return DerivativeSlot(
            asset_id=asset_id,
            slot=slot,
            storage_key=row["storage_key"],
            metadata=json.loads(row["metadata_json"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def create_job(self, job: StoredJob) -> StoredJob:
        if job.project_id is not None and self.load_project(job.project_id) is None:
            raise MissingProjectError(f"unknown project: {job.project_id}")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, project_id, job_type, state, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.project_id,
                    job.job_type,
                    job.state,
                    _strict_json(job.payload),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
        return job

    def get_job(self, job_id: str) -> StoredJob | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return StoredJob(
            job_id=row["job_id"],
            project_id=row["project_id"],
            job_type=row["job_type"],
            state=row["state"],
            payload=json.loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def update_job_state(self, job_id: str, state: str) -> StoredJob:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown job: {job_id}")

            updated = StoredJob(
                job_id=row["job_id"],
                project_id=row["project_id"],
                job_type=row["job_type"],
                state=state,
                payload=json.loads(row["payload_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.now(timezone.utc),
            )
            changed = connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (updated.state, updated.updated_at.isoformat(), job_id),
            ).rowcount
            if changed != 1:
                raise StorageError(f"job disappeared during update: {job_id}")
        return updated
