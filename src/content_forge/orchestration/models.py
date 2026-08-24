"""Validated contracts for persistent render jobs and artifact sidecars."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Literal, Mapping, Self

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from content_forge.core import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel

RENDER_ARTIFACT_MANIFEST_VERSION = "1.0"
RENDER_FAILURE_MANIFEST_VERSION = "1.0"
RenderArtifactManifestVersion = Literal["1.0"]
RenderFailureManifestVersion = Literal["1.0"]
RenderPurpose = Literal["preview", "final"]

Digest = Annotated[
    str,
    StringConstraints(
        to_lower=True,
        strip_whitespace=True,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    ),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _storage_key(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("storage key must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("storage key must be a canonical POSIX relative path")
    if str(path) != value:
        raise ValueError("storage key must be canonical")
    return value


class RenderSourceFingerprint(FrozenModel):
    """Source identity copied from the frozen RenderPlan asset table."""

    asset_id: str
    sha256: Digest
    storage_key: str | None = Field(default=None, max_length=1024)

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.ASSET)

    @field_validator("storage_key")
    @classmethod
    def validate_storage_key(cls, value: str | None) -> str | None:
        return None if value is None else _storage_key(value)


class RenderArtifactManifest(FrozenModel):
    """Successful render sidecar written before a job becomes succeeded."""

    artifact_manifest_version: RenderArtifactManifestVersion = (
        RENDER_ARTIFACT_MANIFEST_VERSION
    )
    job_id: str
    project_id: str
    purpose: RenderPurpose
    profile_id: RegistryKey
    variant_id: str | None = None
    variant_language: str | None = Field(default=None, min_length=1, max_length=64)
    template_id: RegistryKey | None = None
    template_version: str | None = Field(default=None, min_length=1, max_length=64)
    render_plan_digest: Digest
    command_manifest_digest: Digest
    command_manifest_storage_key: str = Field(min_length=1, max_length=1024)
    output_sha256: Digest
    output_storage_key: str = Field(min_length=1, max_length=1024)
    manifest_storage_key: str = Field(min_length=1, max_length=1024)
    video_encoder: str = Field(min_length=1, max_length=128)
    ffmpeg_version: str = Field(min_length=1, max_length=4096)
    bytes_written: int = Field(ge=1)
    elapsed_seconds: float = Field(ge=0.0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    duration_seconds: float = Field(gt=0.0)
    fps: float | None = Field(default=None, gt=0.0)
    has_audio: bool
    video_codec: str | None = Field(default=None, max_length=128)
    audio_codec: str | None = Field(default=None, max_length=128)
    source_assets: tuple[RenderSourceFingerprint, ...] = ()
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("variant_id")
    @classmethod
    def validate_variant_id(cls, value: str | None) -> str | None:
        if value is not None:
            return require_entity_id(value, EntityKind.VARIANT)
        return value

    @field_validator(
        "command_manifest_storage_key",
        "output_storage_key",
        "manifest_storage_key",
    )
    @classmethod
    def validate_storage_keys(cls, value: str) -> str:
        return _storage_key(value)

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_identity_pairs(self) -> Self:
        if (self.variant_id is None) != (self.variant_language is None):
            raise ValueError("variant ID/language must be present together")
        if (self.template_id is None) != (self.template_version is None):
            raise ValueError("template ID/version must be present together")
        asset_ids = [item.asset_id for item in self.source_assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("render artifact source asset IDs must be unique")
        if tuple(asset_ids) != tuple(sorted(asset_ids)):
            raise ValueError("render artifact source assets must be sorted by asset ID")
        return self


class RenderFailureManifest(FrozenModel):
    """Bounded diagnostic sidecar for a failed or cancelled render attempt."""

    failure_manifest_version: RenderFailureManifestVersion = (
        RENDER_FAILURE_MANIFEST_VERSION
    )
    job_id: str
    project_id: str
    purpose: RenderPurpose
    profile_id: RegistryKey
    render_plan_digest: Digest
    failure_storage_key: str = Field(min_length=1, max_length=1024)
    state: Literal["failed", "cancelled"]
    code: str = Field(min_length=1, max_length=128)
    stage: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8192)
    exception_type: str = Field(min_length=1, max_length=512)
    return_code: int | None = None
    details: Mapping[str, JsonValue] = Field(default_factory=dict)
    failed_at: datetime = Field(default_factory=utc_now)

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("failure_storage_key")
    @classmethod
    def validate_failure_storage_key(cls, value: str) -> str:
        return _storage_key(value)

    @field_validator("failed_at")
    @classmethod
    def validate_failed_at(cls, value: datetime) -> datetime:
        return _aware(value)
