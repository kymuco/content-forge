"""Schema version contract for persisted Content Forge models."""

from typing import Literal

CURRENT_SCHEMA_VERSION = "1.0"
SchemaVersion = Literal["1.0"]

SUPPORTED_SCHEMA_VERSIONS = frozenset({CURRENT_SCHEMA_VERSION})


def is_supported_schema_version(value: str) -> bool:
    """Return whether *value* can be loaded directly by this build."""

    return value in SUPPORTED_SCHEMA_VERSIONS
