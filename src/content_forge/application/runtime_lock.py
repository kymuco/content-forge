"""Cross-process advisory ownership for one Content Forge API runtime root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class RuntimeBusyError(RuntimeError):
    """Raised when another API process already owns the same runtime root."""


class RuntimeLease:
    """Exclusive advisory file lock held for the lifetime of one API process.

    The lock file itself may survive a crash. Ownership does not: the operating system
    releases the advisory lock when the process/file descriptor disappears, so a new
    process can immediately recover durable Inbox receipts without a stale timeout.
    """

    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle: BinaryIO | None = handle

    @classmethod
    def acquire(cls, path: str | Path) -> "RuntimeLease":
        lock_path = Path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeBusyError(
                        f"Content Forge runtime is already owned: {lock_path}"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeBusyError(
                        f"Content Forge runtime is already owned: {lock_path}"
                    ) from exc
            return cls(lock_path, handle)
        except BaseException:
            handle.close()
            raise

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "RuntimeLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
