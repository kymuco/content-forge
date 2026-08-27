"""Packaged PWA resources for the Content Forge local application surface."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def static_path(relative_name: str) -> Path:
    """Resolve a fixed packaged PWA asset to an unpacked installation path."""

    if not relative_name or relative_name.startswith(("/", "\\")) or ".." in Path(relative_name).parts:
        raise ValueError("invalid static asset path")
    resource = files("content_forge.web").joinpath("static", relative_name)
    path = Path(str(resource))
    if not path.is_file():
        raise FileNotFoundError(relative_name)
    return path


__all__ = ["static_path"]
