"""Convenient façade for the local Content Forge library."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from content_forge.core import Project

from .asset_store import AssetStore
from .database import LibraryDatabase
from .library_index_hardening import ProductionLibraryIndex
from .paths import RuntimePaths

if TYPE_CHECKING:
    from .publishing_hardening import PublishingRepository


class LocalLibrary:
    """Bundle runtime paths, SQLite metadata and immutable asset storage."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.paths = RuntimePaths.from_root(root).ensure()
        self.database = LibraryDatabase(self.paths.database).initialize()
        self.assets = AssetStore(self.paths, self.database)
        self._index: ProductionLibraryIndex | None = None
        self._publishing: PublishingRepository | None = None

    @property
    def index(self) -> ProductionLibraryIndex:
        """Open the additive PR26 index only when production-library features are used."""

        if self._index is None:
            self._index = ProductionLibraryIndex(self.database).initialize()
        return self._index

    @property
    def publishing(self) -> PublishingRepository:
        """Open the additive PR27 publishing ledger only when publishing is used."""

        if self._publishing is None:
            from .publishing_hardening import PublishingRepository

            self._publishing = PublishingRepository(self.database).initialize()
        return self._publishing

    def save_project(self, project: Project) -> Project:
        return self.database.save_project(project)

    def load_project(self, project_id: str) -> Project | None:
        return self.database.load_project(project_id)
