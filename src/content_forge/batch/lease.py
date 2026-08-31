"""Cross-process live ownership for one PR17 batch run."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class BatchLeaseBusyError(RuntimeError):
    """Another live process currently owns this batch execution."""


class BatchRunLease:
    """Non-blocking OS advisory lock released automatically when a process dies."""

    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle: BinaryIO | None = handle

    @classmethod
    def acquire(cls, path: str | Path) -> "BatchRunLease":
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
                    raise BatchLeaseBusyError(
                        f"batch is already owned by a live runner: {lock_path}"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise BatchLeaseBusyError(
                        f"batch is already owned by a live runner: {lock_path}"
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

    def __enter__(self) -> "BatchRunLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = ["BatchLeaseBusyError", "BatchRunLease"]
