"""Convenient façade for the local Content Forge library."""

from __future__ import annotations

from pathlib import Path

from content_forge.core import Project

from .asset_store import AssetStore
from .database import LibraryDatabase
from .library_index_hardening import ProductionLibraryIndex
from .paths import RuntimePaths


class LocalLibrary:
    """Bundle runtime paths, SQLite metadata and immutable asset storage."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.paths = RuntimePaths.from_root(root).ensure()
        self.database = LibraryDatabase(self.paths.database).initialize()
        self.assets = AssetStore(self.paths, self.database)
        self.index = ProductionLibraryIndex(self.database).initialize()

    def save_project(self, project: Project) -> Project:
        return self.database.save_project(project)

    def load_project(self, project_id: str) -> Project | None:
        return self.database.load_project(project_id)
