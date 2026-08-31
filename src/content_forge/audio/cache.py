"""Small content-addressed cache boundary for PR14 mastered audio intermediates."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_HEX = frozenset("0123456789abcdef")


def _validate_key(value: str) -> str:
    if len(value) != 64 or any(char not in _HEX for char in value):
        raise ValueError("audio intermediate cache key must be a lowercase SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class AudioIntermediateCache:
    root: Path

    def path_for(self, key: str, *, suffix: str = ".wav") -> Path:
        digest = _validate_key(key)
        if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
            raise ValueError("audio cache suffix must be a simple extension")
        return self.root / digest[:2] / f"{digest}{suffix}"

    def has(self, key: str, *, suffix: str = ".wav") -> bool:
        return self.path_for(key, suffix=suffix).is_file()

    def publish(
        self,
        source: str | Path,
        key: str,
        *,
        suffix: str = ".wav",
    ) -> Path:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        destination = self.path_for(key, suffix=suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            return destination

        temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
        try:
            with source_path.open("rb") as incoming, temporary.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            os.replace(temporary, destination)
            if os.name == "posix":
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination


__all__ = ["AudioIntermediateCache"]
