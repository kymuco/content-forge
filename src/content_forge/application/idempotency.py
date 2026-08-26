"""Request-scoped idempotency identity for durable Inbox capture retries."""

from __future__ import annotations

import hashlib
import re
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
    """Raised when one request key is reused for different capture metadata."""


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


@contextmanager
def intake_idempotency_scope(key: str | None) -> Iterator[None]:
    """Apply a validated retry identity only while one capture call is executing."""

    normalized = None if key is None else normalize_idempotency_key(key)
    token = _CURRENT_IDEMPOTENCY_KEY.set(normalized)
    try:
        yield
    finally:
        _CURRENT_IDEMPOTENCY_KEY.reset(token)


def same_intake_request(existing: InboxIntake, candidate: InboxIntake) -> bool:
    """Compare only caller-controlled immutable capture metadata, not later checkpoints."""

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
