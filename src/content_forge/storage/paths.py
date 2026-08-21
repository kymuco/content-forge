"""Local runtime path policy.

Runtime media and SQLite state are intentionally outside the source repository by
default. Tests and callers may still provide an explicit temporary root.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_runtime_root() -> Path:
    override = os.environ.get("CONTENT_FORGE_HOME")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "ContentForge"
        return Path.home() / "AppData" / "Local" / "ContentForge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ContentForge"
    return Path.home() / ".local" / "share" / "content-forge"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    assets: Path
    incoming: Path
    database: Path

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> "RuntimePaths":
        base = default_runtime_root() if root is None else Path(root).expanduser()
        return cls(
            root=base,
            assets=base / "assets" / "sha256",
            incoming=base / "assets" / ".incoming",
            database=base / "content-forge.sqlite3",
        )

    def ensure(self) -> "RuntimePaths":
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets.mkdir(parents=True, exist_ok=True)
        self.incoming.mkdir(parents=True, exist_ok=True)
        return self

    def storage_key_for_sha256(self, digest: str) -> str:
        return f"assets/sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def blob_path_for_sha256(self, digest: str) -> Path:
        return self.root / self.storage_key_for_sha256(digest)
