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


def fsync_directory_chain(path: str | Path, *, stop_at: str | Path) -> None:
    """Make directory entries durable from ``path`` through ``stop_at`` on POSIX.

    Fsyncing a newly-written file is not enough to guarantee that a rename or newly
    created directory hierarchy survives power loss. Callers publish into sharded runtime
    directories, so sync every directory in that chain after the final atomic rename and
    before committing the corresponding SQLite receipt. Windows has no portable Python
    directory-fsync primitive, so this is intentionally a no-op there.
    """

    if os.name == "nt":
        return

    current = Path(path).resolve()
    boundary = Path(stop_at).resolve()
    if current != boundary and boundary not in current.parents:
        raise ValueError("directory durability path must be inside stop_at")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    while True:
        descriptor = os.open(current, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if current == boundary:
            break
        current = current.parent


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
        # On POSIX, remember the nearest already-existing ancestor before recursive
        # creation. Fsyncing only ``root`` after mkdir does not persist ``root``'s own
        # directory entry, which belongs to its parent; when parents=True creates more
        # than one directory, each newly-created entry must be covered through the first
        # pre-existing ancestor. Windows has no portable Python directory-fsync primitive.
        durability_boundary = self.root
        if os.name != "nt":
            while not durability_boundary.exists():
                parent = durability_boundary.parent
                if parent == durability_boundary:
                    break
                durability_boundary = parent

        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            fsync_directory_chain(self.root, stop_at=durability_boundary)

        self.assets.mkdir(parents=True, exist_ok=True)
        self.incoming.mkdir(parents=True, exist_ok=True)
        return self

    def storage_key_for_sha256(self, digest: str) -> str:
        return f"assets/sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def blob_path_for_sha256(self, digest: str) -> Path:
        return self.root / self.storage_key_for_sha256(digest)
