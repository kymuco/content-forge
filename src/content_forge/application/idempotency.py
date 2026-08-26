"""Request-scoped idempotency identity for durable Inbox capture retries."""

from __future__ import annotations

import hashlib
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from .models import InboxIntake

_IDEMPOTENCY_KEY_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CURRENT_IDEMPOTENCY_KEY: ContextVar[str | None] = ContextVar(
    "content_forge_intake_idempotency_key",
    default=None,
)
# The API runtime is single-owner for one library root, but FastAPI sync handlers can
# execute concurrently in its thread pool. A fixed lock stripe set serializes duplicate
# request keys without retaining an unbounded per-capture lock dictionary for the process
# lifetime. Unrelated keys can rarely share a stripe; that costs latency only and cannot
# merge their independent durable identities.
_IDEMPOTENCY_LOCKS = tuple(threading.Lock() for _ in range(128))
_REQUEST_FIELDS = (
    "kind",
    "original_name",
    "mime_type",
    "source_url",
    "note",
    "creator_hint",
    "content_kind_hint",
)


class IdempotencyReplay(Exception):
    """Signal that a durable intake already represents this request key."""

    def __init__(self, intake: InboxIntake) -> None:
        super().__init__(f"idempotent replay for {intake.intake_id}")
        self.intake = intake


class IdempotencyConflict(RuntimeError):
    """Raised when one request key is reused for different capture input."""


def normalize_idempotency_key(value: str) -> str:
    normalized = value.strip().lower()
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise ValueError("Idempotency-Key must be a canonical UUID")
    return normalized


def intake_id_for_key(key: str) -> str:
    """Map one stable client queue UUID onto one stable application intake identity."""

    normalized = normalize_idempotency_key(key)
    digest = hashlib.sha256(
        f"content-forge-intake-v1\0{normalized}".encode("utf-8")
    ).hexdigest()
    return f"cf_intake_{digest[:32]}"


def current_idempotency_key() -> str | None:
    return _CURRENT_IDEMPOTENCY_KEY.get()


def _lock_for_key(normalized: str) -> threading.Lock:
    digest = hashlib.sha256(
        f"content-forge-idempotency-lock-v1\0{normalized}".encode("utf-8")
    ).digest()
    slot = int.from_bytes(digest[:4], "big") % len(_IDEMPOTENCY_LOCKS)
    return _IDEMPOTENCY_LOCKS[slot]


@contextmanager
def intake_idempotency_scope(key: str | None) -> Iterator[None]:
    """Apply one retry identity and serialize its side-effect execution in-process.

    The SQLite primary key is the durable cross-process identity. This lock is a narrower
    live-process guard: two concurrent HTTP retries for the same queue record must not
    both observe the same RECEIVING record and independently advance its checkpoints.
    After process interruption the lock disappears; exclusive startup reconciliation
    remains authoritative for any durable RECEIVING state.
    """

    normalized = None if key is None else normalize_idempotency_key(key)
    lock = None if normalized is None else _lock_for_key(normalized)
    if lock is not None:
        lock.acquire()
    token = _CURRENT_IDEMPOTENCY_KEY.set(normalized)
    try:
        yield
    finally:
        _CURRENT_IDEMPOTENCY_KEY.reset(token)
        if lock is not None:
            lock.release()


def same_intake_request(existing: InboxIntake, candidate: InboxIntake) -> bool:
    """Compare caller-controlled metadata; accepted file bytes are checked separately."""

    return all(getattr(existing, field) == getattr(candidate, field) for field in _REQUEST_FIELDS)


__all__ = [
    "IdempotencyConflict",
    "IdempotencyReplay",
    "current_idempotency_key",
    "intake_id_for_key",
    "intake_idempotency_scope",
    "normalize_idempotency_key",
    "same_intake_request",
]
