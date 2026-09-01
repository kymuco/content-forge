"""PR21 voice-cast value objects and project manifest."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from content_forge.application.tts import LineTTSSettings
from content_forge.core import Project
from content_forge.core.ids import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, SHA256

CAST_DEFINITION_VERSION = "pr21_voice_cast_definition_v1"
CAST_REVISION_VERSION = "pr21_voice_cast_revision_v1"
CAST_MANIFEST_VERSION = "pr21_voice_cast_manifest_v1"
CAST_METADATA_KEY = "pr21_voice_cast"
MAX_CAST_ENTRIES = 1024
MAX_PROJECT_BINDINGS = 256


class VoiceCastError(RuntimeError):
    pass


class VoiceCastConflictError(VoiceCastError):
    pass


class VoiceCastNotFoundError(VoiceCastError):
    pass


class VoiceCastValidationError(VoiceCastError):
    pass


class VoiceCastUnavailableError(VoiceCastError):
    pass


def cast_definition_digest(
    *,
    cast_id: str,
    display_name: str,
    settings: LineTTSSettings,
    reference_audio_sha256: str | None,
) -> str:
    encoded = json.dumps(
        {
            "cast_id": cast_id,
            "display_name": display_name,
            "settings": settings.model_dump(mode="json"),
            "reference_audio_sha256": reference_audio_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class VoiceCastDefinition(FrozenModel):
    """Reusable semantic voice recipe, distinct from PR19 narrative identity."""

    contract_version: Literal["pr21_voice_cast_definition_v1"] = CAST_DEFINITION_VERSION
    cast_id: RegistryKey
    display_name: str = Field(min_length=1, max_length=512)
    settings: LineTTSSettings

    @field_validator("display_name")
    @classmethod
    def non_blank_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("voice cast display_name must contain non-whitespace content")
        return value


class VoiceCastRevision(FrozenModel):
    """Immutable persisted revision of one runtime-wide cast voice."""

    contract_version: Literal["pr21_voice_cast_revision_v1"] = CAST_REVISION_VERSION
    cast_id: RegistryKey
    revision: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=512)
    settings: LineTTSSettings
    reference_audio_sha256: SHA256 | None = None
    definition_sha256: SHA256
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("voice cast created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def definition_digest_matches(self):
        has_reference = self.settings.reference_asset_id is not None
        if has_reference != (self.reference_audio_sha256 is not None):
            raise ValueError(
                "voice cast revision reference asset and reference digest must be retained together"
            )
        expected = cast_definition_digest(
            cast_id=self.cast_id,
            display_name=self.display_name,
            settings=self.settings,
            reference_audio_sha256=self.reference_audio_sha256,
        )
        if self.definition_sha256 != expected:
            raise ValueError("voice cast revision definition digest mismatch")
        return self


class CharacterCastBinding(FrozenModel):
    """Project mapping from one PR19 character to one immutable cast revision."""

    character_id: RegistryKey
    cast_id: RegistryKey
    cast_revision: int = Field(ge=1)
    cast_definition_sha256: SHA256
    settings_override: LineTTSSettings | None = None
    settings_override_reference_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def pin_override_reference(self):
        has_override_reference = (
            self.settings_override is not None
            and self.settings_override.reference_asset_id is not None
        )
        if has_override_reference != (self.settings_override_reference_sha256 is not None):
            raise ValueError(
                "voice cast override reference asset and reference digest must be retained together"
            )
        return self


class ProjectVoiceCastManifest(FrozenModel):
    contract_version: Literal["pr21_voice_cast_manifest_v1"] = CAST_MANIFEST_VERSION
    project_id: str
    bindings: tuple[CharacterCastBinding, ...] = Field(
        default=(), max_length=MAX_PROJECT_BINDINGS
    )

    @model_validator(mode="after")
    def validate_manifest(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        ids = tuple(item.character_id for item in self.bindings)
        if len(set(ids)) != len(ids):
            raise ValueError("voice cast character bindings must be unique")
        return self


class ResolvedLineVoice(FrozenModel):
    """Exact cast evidence and effective PR20 settings for one dialogue line."""

    project_id: str
    scene_id: str
    line_id: str
    character_id: RegistryKey
    cast_id: RegistryKey
    cast_revision: int = Field(ge=1)
    cast_definition_sha256: SHA256
    settings: LineTTSSettings
    reference_audio_sha256: SHA256 | None = None
    override_applied: bool

    @model_validator(mode="after")
    def validate_ids_and_reference(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        require_entity_id(self.scene_id, EntityKind.SCENE)
        has_reference = self.settings.reference_asset_id is not None
        if has_reference != (self.reference_audio_sha256 is not None):
            raise ValueError(
                "resolved voice reference asset and reference digest must be retained together"
            )
        return self


def _plain_metadata(project: Project) -> dict[str, object]:
    metadata = project.model_dump(mode="json")["metadata"]
    if not isinstance(metadata, dict):  # pragma: no cover - core Project contract
        raise VoiceCastValidationError("project metadata is malformed")
    return metadata


def voice_cast_manifest(project: Project) -> ProjectVoiceCastManifest:
    raw = _plain_metadata(project).get(CAST_METADATA_KEY)
    if raw is None:
        return ProjectVoiceCastManifest(project_id=project.project_id)
    if not isinstance(raw, Mapping):
        raise VoiceCastValidationError("stored PR21 voice cast metadata is malformed")
    try:
        manifest = ProjectVoiceCastManifest.model_validate(raw)
    except Exception as exc:
        raise VoiceCastValidationError("stored PR21 voice cast manifest is malformed") from exc
    if manifest.project_id != project.project_id:
        raise VoiceCastConflictError("voice cast manifest project identity mismatch")
    return manifest


def project_metadata_copy(project: Project) -> dict[str, object]:
    return _plain_metadata(project)


__all__ = [
    "CAST_METADATA_KEY",
    "MAX_CAST_ENTRIES",
    "CharacterCastBinding",
    "ProjectVoiceCastManifest",
    "ResolvedLineVoice",
    "VoiceCastConflictError",
    "VoiceCastDefinition",
    "VoiceCastError",
    "VoiceCastNotFoundError",
    "VoiceCastRevision",
    "VoiceCastUnavailableError",
    "VoiceCastValidationError",
    "cast_definition_digest",
    "project_metadata_copy",
    "voice_cast_manifest",
]
