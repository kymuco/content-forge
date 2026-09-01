"""Atomic and Unicode-correct hardening for the PR26 production-library index."""

from __future__ import annotations

import unicodedata

from . import library_index as _base

_MAX_QUERY_TAGS = 128


def _normalize_tag_value(value: str) -> str:
    """Mirror the PR26 normalization contract while rejecting non-scalar text."""

    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("library tag value must not be empty")
    if len(normalized) > 256:
        raise ValueError("library tag value exceeds 256 characters")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in normalized):
        raise ValueError("library tag value contains control/format/surrogate characters")
    return normalized


def _prefix_upper_bound(prefix: str) -> str | None:
    """Return the smallest valid Unicode string strictly above every prefix match.

    SQLite's BINARY collation preserves valid UTF-8 scalar ordering. Incrementing the
    right-most scalar that still has a successor yields an index-friendly half-open
    range without assuming U+FFFF is the largest possible suffix character.
    """

    characters = list(prefix)
    for index in range(len(characters) - 1, -1, -1):
        codepoint = ord(characters[index])
        if codepoint >= 0x10FFFF:
            continue
        successor = codepoint + 1
        if successor == 0xD800:
            successor = 0xE000
        return "".join(characters[:index]) + chr(successor)
    return None


def _require_query_bound(query: _base.LibrarySearchQuery) -> None:
    if len(query.tags) > _MAX_QUERY_TAGS:
        raise ValueError(f"library search query exceeds {_MAX_QUERY_TAGS} exact tags")


# Base Pydantic validators resolve this module global at validation time. Replace the
# original helper once the public hardened storage surface loads so malformed lone
# surrogates fail as validation errors before they can reach sqlite3's UTF-8 encoder.
_base._normalize_tag_value = _normalize_tag_value


class ProductionLibraryIndex(_base.ProductionLibraryIndex):
    """Final PR26 index surface with atomic schema setup and bounded Unicode search."""

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
                (_base._PRODUCTION_LIBRARY_SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > _base._PRODUCTION_LIBRARY_SCHEMA_VERSION:
                raise _base.StorageSchemaError(
                    "production library schema "
                    f"{version} is newer than supported {_base._PRODUCTION_LIBRARY_SCHEMA_VERSION}"
                )
            if version not in {0, _base._PRODUCTION_LIBRARY_SCHEMA_VERSION}:
                raise _base.StorageSchemaError(
                    "unsupported production library schema migration: "
                    f"{version} -> {_base._PRODUCTION_LIBRARY_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS library_asset_tags (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    value_key TEXT NOT NULL,
                    display_value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (asset_id, kind, value_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_library_asset_tags_lookup
                ON library_asset_tags(kind, value_key, asset_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_library_asset_tags_value
                ON library_asset_tags(value_key, asset_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS library_virtual_collections (
                    collection_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    query_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            if version == 0:
                connection.execute(
                    "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                    (
                        _base._PRODUCTION_LIBRARY_SCHEMA_COMPONENT,
                        _base._PRODUCTION_LIBRARY_SCHEMA_VERSION,
                    ),
                )
        return self

    def search(self, query: _base.LibrarySearchQuery) -> tuple[_base.LibrarySearchHit, ...]:
        _require_query_bound(query)
        if query.tag_prefix is None:
            return super().search(query)

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

        prefix = _base._normalize_prefix(query.tag_prefix)
        upper_bound = _prefix_upper_bound(prefix)
        if upper_bound is None:
            conditions.append(
                """
                EXISTS (
                    SELECT 1 FROM library_asset_tags prefix_tag
                    WHERE prefix_tag.asset_id = a.asset_id
                      AND prefix_tag.value_key >= ?
                )
                """
            )
            parameters.append(prefix)
        else:
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
            parameters.extend((prefix, upper_bound))

        if query.previously_used is True:
            conditions.append(
                "EXISTS (SELECT 1 FROM project_assets used WHERE used.asset_id = a.asset_id)"
            )
        elif query.previously_used is False:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM project_assets used WHERE used.asset_id = a.asset_id)"
            )

        where_sql = " AND ".join(conditions)
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
            tags_by_asset: dict[str, list[_base.LibraryTag]] = {
                asset_id: [] for asset_id in asset_ids
            }
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
                        _base.LibraryTag(
                            kind=str(row["kind"]),
                            value=str(row["display_value"]),
                        )
                    )

        return tuple(
            _base.LibrarySearchHit(
                asset=_base.Asset.model_validate_json(str(row["manifest_json"])),
                tags=tuple(tags_by_asset[str(row["asset_id"])]),
                source_count=int(row["source_count"]),
                project_count=int(row["project_count"]),
            )
            for row in rows
        )

    def put_collection(
        self,
        collection_id: _base.RegistryKey,
        name: str,
        query: _base.LibrarySearchQuery,
    ) -> _base.VirtualCollection:
        _require_query_bound(query)
        return super().put_collection(collection_id, name, query)


__all__ = ["ProductionLibraryIndex"]
