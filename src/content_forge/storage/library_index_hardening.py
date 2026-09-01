"""Atomic initialization hardening for the PR26 production-library index."""

from __future__ import annotations

from . import library_index as _base


class ProductionLibraryIndex(_base.ProductionLibraryIndex):
    """Final PR26 index surface with one explicit transaction for feature schema setup."""

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


__all__ = ["ProductionLibraryIndex"]
