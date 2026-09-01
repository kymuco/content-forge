"""Stable identifier helpers.

Entity IDs are opaque, globally unique strings. Human-facing registry identifiers such
as template IDs are deliberately separate and remain readable.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import StringConstraints

ENTITY_ID_PATTERN = re.compile(
    r"^cf_(asset|source|project|variant|scene|overlay|audio|review|suggestion|job|publish)_[0-9a-f]{32}$"
)
REGISTRY_KEY_REGEX = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"

RegistryKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=REGISTRY_KEY_REGEX,
    ),
]


class EntityKind(StrEnum):
    ASSET = "asset"
    SOURCE = "source"
    PROJECT = "project"
    VARIANT = "variant"
    SCENE = "scene"
    OVERLAY = "overlay"
    AUDIO = "audio"
    REVIEW = "review"
    SUGGESTION = "suggestion"
    JOB = "job"
    PUBLISH = "publish"


def new_entity_id(kind: EntityKind) -> str:
    """Create an opaque stable ID with a readable entity prefix."""

    return f"cf_{kind.value}_{uuid4().hex}"


def require_entity_id(value: str, kind: EntityKind) -> str:
    """Validate *value* and ensure it has the expected entity prefix."""

    if not ENTITY_ID_PATTERN.fullmatch(value):
        raise ValueError(f"invalid Content Forge entity ID: {value!r}")
    expected = f"cf_{kind.value}_"
    if not value.startswith(expected):
        raise ValueError(f"expected {kind.value} ID, got {value!r}")
    return value
