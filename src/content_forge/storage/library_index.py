"""PR26 production-library tagging, search, virtual collections, and reuse history."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from content_forge.core import Asset, EntityKind, RegistryKey, require_entity_id

from .database import LibraryDatabase, MissingAssetError, StorageSchemaError

_PRODUCTION_LIBRARY_SCHEMA_COMPONENT = "production_library"
_PRODUCTION_LIBRARY_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class LibraryTagKind(StrEnum):
    GAME = "game"
    ANIME = "anime"
    ARTIST = "artist"
    CHARACTER = "character"
    TOPIC = "topic"
    SOURCE = "source"


def _normalize_tag_value(value: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("library tag value must not be empty")
    if len(normalized) > 256:
        raise ValueError("library tag value exceeds 256 characters")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in normalized):
        raise ValueError("library tag value contains control/format characters")
    return normalized


def _normalize_prefix(value: str) -> str:
    normalized = _normalize_tag_value(value).casefold()
    if len(normalized) > 512:
        raise ValueError("library tag prefix is too long")
    return normalized


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class LibraryTag(_FrozenModel):
    kind: LibraryTagKind
    value: str = Field(min_length=1, max_length=256)

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return _normalize_tag_value(value)

    @property
    def value_key(self) -> str:
        return self.value.casefold()


class LibrarySearchQuery(_FrozenModel):
    """Indexed library query. All explicit tags use AND semantics."""

    tags: tuple[LibraryTag, ...] = ()
    tag_prefix: str | None = Field(default=None, max_length=256)
    previously_used: bool | None = None
    limit: int = Field(default=50, ge=1, le=256)
    offset: int = Field(default=0, ge=0, le=100_000)

    @field_validator("tag_prefix")
    @classmethod
    def normalize_tag_prefix(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_tag_value(value)

    @model_validator(mode="after")
    def unique_tags(self):
        keys = [(item.kind.value, item.value_key) for item in self.tags]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate library search tag")
        return self


class LibrarySearchHit(_FrozenModel):
    asset: Asset
    tags: tuple[LibraryTag, ...]
    source_count: int = Field(ge=0)
    project_count: int = Field(ge=0)

    @property
    def previously_used(self) -> bool:
        return self.project_count > 0

    @property
    def has_multiple_sources(self) -> bool:
        return self.source_count > 1


class LibraryDuplicateInfo(_FrozenModel):
    asset: Asset
    source_count: int = Field(ge=0)
    project_count: int = Field(ge=0)

    @property
    def previously_used(self) -> bool:
        return self.project_count > 0

    @property
    def has_multiple_sources(self) -> bool:
        return self.source_count > 1


class LibraryReuseRecord(_FrozenModel):
    project_id: str
    content_kind: str
    project_state: str
    source_id: str | None = None
    role: str
    project_updated_at: datetime

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str | None) -> str | None:
        if value is not None:
            return require_entity_id(value, EntityKind.SOURCE)
        return value


class VirtualCollection(_FrozenModel):
    collection_id: RegistryKey
    name: str = Field(min_length=1, max_length=256)
    query: LibrarySearchQuery
    created_at: datetime
    updated_at: datetime

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("virtual collection name must not be empty")
        return normalized


class ProductionLibraryIndex:
    """Mutable organization metadata over immutable assets/provenance and live Project usage."""

    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

    def initialize(self) -> "ProductionLibraryIndex":
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
                (_PRODUCTION_LIBRARY_SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > _PRODUCTION_LIBRARY_SCHEMA_VERSION:
                raise StorageSchemaError(
                    "production library schema "
                    f"{version} is newer than supported {_PRODUCTION_LIBRARY_SCHEMA_VERSION}"
                )
            if version not in {0, _PRODUCTION_LIBRARY_SCHEMA_VERSION}:
                raise StorageSchemaError(
                    "unsupported production library schema migration: "
                    f"{version} -> {_PRODUCTION_LIBRARY_SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS library_asset_tags (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    value_key TEXT NOT NULL,
                    display_value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (asset_id, kind, value_key)
                );
                CREATE INDEX IF NOT EXISTS idx_library_asset_tags_lookup
                    ON library_asset_tags(kind, value_key, asset_id);
                CREATE INDEX IF NOT EXISTS idx_library_asset_tags_value
                    ON library_asset_tags(value_key, asset_id);

                CREATE TABLE IF NOT EXISTS library_virtual_collections (
                    collection_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    query_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            if version == 0:
                connection.execute(
                    "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                    (_PRODUCTION_LIBRARY_SCHEMA_COMPONENT, _PRODUCTION_LIBRARY_SCHEMA_VERSION),
                )
        return self

    def _require_asset(self, asset_id: str) -> Asset:
        try:
            require_entity_id(asset_id, EntityKind.ASSET)
        except ValueError as exc:
            raise MissingAssetError(f"invalid asset ID: {asset_id}") from exc
        asset = self.database.get_asset(asset_id)
        if asset is None:
            raise MissingAssetError(f"unknown asset: {asset_id}")
        return asset

    def set_tags(self, asset_id: str, tags: tuple[LibraryTag, ...]) -> tuple[LibraryTag, ...]:
        self._require_asset(asset_id)
        canonical: dict[tuple[str, str], LibraryTag] = {}
        for tag in tags:
            key = (tag.kind.value, tag.value_key)
            current = canonical.get(key)
            if current is None or tag.value < current.value:
                canonical[key] = tag
        ordered = tuple(canonical[key] for key in sorted(canonical))
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM library_asset_tags WHERE asset_id = ?", (asset_id,))
            connection.executemany(
                """
                INSERT INTO library_asset_tags(
                    asset_id, kind, value_key, display_value, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (asset_id, tag.kind.value, tag.value_key, tag.value, now)
                    for tag in ordered
                ],
            )
        return ordered

    def tags_for_asset(self, asset_id: str) -> tuple[LibraryTag, ...]:
        self._require_asset(asset_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT kind, display_value FROM library_asset_tags
                WHERE asset_id = ? ORDER BY kind, value_key
                """,
                (asset_id,),
            ).fetchall()
        return tuple(
            LibraryTag(kind=str(row["kind"]), value=str(row["display_value"]))
            for row in rows
        )

    def search(self, query: LibrarySearchQuery) -> tuple[LibrarySearchHit, ...]:
        conditions: list[str] = []
        parameters: list[object] = []
        for tag in query.tags:
            conditions.append(
                """
                EXISTS (
                    SELECT 1 FROM library_asset_tags required_tag
                    WHERE required_tag.asset_id = a.asset_id
                      AND required_tag.kind = ?
                      AND required_tag.value_key = ?
                )
                """
            )
            parameters.extend((tag.kind.value, tag.value_key))
        if query.tag_prefix is not None:
            prefix = _normalize_prefix(query.tag_prefix)
            conditions.append(
                """
                EXISTS (
                    SELECT 1 FROM library_asset_tags prefix_tag
                    WHERE prefix_tag.asset_id = a.asset_id
                      AND prefix_tag.value_key >= ?
                      AND prefix_tag.value_key < ?
                )
                """
            )
            parameters.extend((prefix, prefix + "\uffff"))
        if query.previously_used is True:
            conditions.append(
                "EXISTS (SELECT 1 FROM project_assets used WHERE used.asset_id = a.asset_id)"
            )
        elif query.previously_used is False:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM project_assets used WHERE used.asset_id = a.asset_id)"
            )
        where_sql = " AND ".join(conditions) if conditions else "1 = 1"
        parameters.extend((query.limit, query.offset))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    a.asset_id,
                    a.manifest_json,
                    (SELECT COUNT(*) FROM sources s WHERE s.asset_id = a.asset_id) AS source_count,
                    (SELECT COUNT(DISTINCT pa.project_id) FROM project_assets pa
                        WHERE pa.asset_id = a.asset_id) AS project_count
                FROM assets a
                WHERE {where_sql}
                ORDER BY a.created_at DESC, a.asset_id
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
            asset_ids = [str(row["asset_id"]) for row in rows]
            tags_by_asset: dict[str, list[LibraryTag]] = {asset_id: [] for asset_id in asset_ids}
            if asset_ids:
                placeholders = ",".join("?" for _ in asset_ids)
                tag_rows = connection.execute(
                    f"""
                    SELECT asset_id, kind, display_value FROM library_asset_tags
                    WHERE asset_id IN ({placeholders})
                    ORDER BY asset_id, kind, value_key
                    """,
                    asset_ids,
                ).fetchall()
                for row in tag_rows:
                    tags_by_asset[str(row["asset_id"])].append(
                        LibraryTag(kind=str(row["kind"]), value=str(row["display_value"]))
                    )
        return tuple(
            LibrarySearchHit(
                asset=Asset.model_validate_json(str(row["manifest_json"])),
                tags=tuple(tags_by_asset[str(row["asset_id"])]),
                source_count=int(row["source_count"]),
                project_count=int(row["project_count"]),
            )
            for row in rows
        )

    def duplicate_info(self, sha256: str) -> LibraryDuplicateInfo | None:
        digest = sha256.strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("invalid SHA-256 digest")
        asset = self.database.get_asset_by_sha256(digest)
        if asset is None:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM sources WHERE asset_id = ?) AS source_count,
                    (SELECT COUNT(DISTINCT project_id) FROM project_assets
                        WHERE asset_id = ?) AS project_count
                """,
                (asset.asset_id, asset.asset_id),
            ).fetchone()
        assert row is not None
        return LibraryDuplicateInfo(
            asset=asset,
            source_count=int(row["source_count"]),
            project_count=int(row["project_count"]),
        )

    def reuse_history(self, asset_id: str) -> tuple[LibraryReuseRecord, ...]:
        self._require_asset(asset_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.project_id,
                    p.content_kind,
                    p.state,
                    p.updated_at,
                    pa.source_id,
                    pa.role
                FROM project_assets pa
                JOIN projects p ON p.project_id = pa.project_id
                WHERE pa.asset_id = ?
                ORDER BY p.updated_at DESC, p.project_id, COALESCE(pa.source_id, ''), pa.role
                """,
                (asset_id,),
            ).fetchall()
        return tuple(
            LibraryReuseRecord(
                project_id=str(row["project_id"]),
                content_kind=str(row["content_kind"]),
                project_state=str(row["state"]),
                source_id=None if row["source_id"] is None else str(row["source_id"]),
                role=str(row["role"]),
                project_updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        )

    @staticmethod
    def _decode_collection(row) -> VirtualCollection:
        return VirtualCollection(
            collection_id=str(row["collection_id"]),
            name=str(row["name"]),
            query=LibrarySearchQuery.model_validate(json.loads(str(row["query_json"]))),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def put_collection(
        self,
        collection_id: RegistryKey,
        name: str,
        query: LibrarySearchQuery,
    ) -> VirtualCollection:
        now = datetime.now(timezone.utc)
        candidate = VirtualCollection(
            collection_id=collection_id,
            name=name,
            query=query,
            created_at=now,
            updated_at=now,
        )
        encoded_query = json.dumps(
            query.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT created_at FROM library_virtual_collections WHERE collection_id = ?",
                (candidate.collection_id,),
            ).fetchone()
            created_at = now if existing is None else datetime.fromisoformat(str(existing["created_at"]))
            stored = candidate.model_copy(update={"created_at": created_at})
            connection.execute(
                """
                INSERT INTO library_virtual_collections(
                    collection_id, name, query_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(collection_id) DO UPDATE SET
                    name = excluded.name,
                    query_json = excluded.query_json,
                    updated_at = excluded.updated_at
                """,
                (
                    stored.collection_id,
                    stored.name,
                    encoded_query,
                    stored.created_at.isoformat(),
                    stored.updated_at.isoformat(),
                ),
            )
        return stored

    def get_collection(self, collection_id: str) -> VirtualCollection | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM library_virtual_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
        return None if row is None else self._decode_collection(row)

    def list_collections(self) -> tuple[VirtualCollection, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM library_virtual_collections ORDER BY collection_id"
            ).fetchall()
        return tuple(self._decode_collection(row) for row in rows)

    def delete_collection(self, collection_id: str) -> bool:
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM library_virtual_collections WHERE collection_id = ?",
                (collection_id,),
            ).rowcount
        return changed == 1

    def search_collection(self, collection_id: str) -> tuple[LibrarySearchHit, ...]:
        collection = self.get_collection(collection_id)
        if collection is None:
            raise KeyError(f"unknown virtual collection: {collection_id}")
        return self.search(collection.query)


__all__ = [
    "LibraryDuplicateInfo",
    "LibraryReuseRecord",
    "LibrarySearchHit",
    "LibrarySearchQuery",
    "LibraryTag",
    "LibraryTagKind",
    "ProductionLibraryIndex",
    "VirtualCollection",
]
