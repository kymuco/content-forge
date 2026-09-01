"""PR27 platform-agnostic publishing provider and approval contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from content_forge.core import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.orchestration import RenderArtifactManifest

_PUBLISH_CONTRACT_VERSION = "pr27_publish_contract_v1"
PublishVisibility = Literal["private", "unlisted", "public"]
PublishDisposition = Literal["published", "scheduled"]


class PublishingProviderError(RuntimeError):
    """Base class for optional publishing-provider failures."""


class PublishingUnavailableError(PublishingProviderError):
    """The provider runtime/account integration is unavailable."""


class PublishingExecutionError(PublishingProviderError):
    """Remote publishing failed before a validated result existed."""


class PublishingResponseError(PublishingProviderError):
    """A provider response violated the PR27 result contract."""


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nonblank(value: str, *, label: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label} must contain non-whitespace content")
    return normalized


class PublishArtifactRef(FrozenModel):
    """Exact authenticated final-render identity used as publish input."""

    project_id: str
    render_job_id: str
    profile_id: RegistryKey
    variant_id: str | None = None
    render_plan_digest: SHA256
    output_sha256: SHA256
    bytes_written: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    duration_seconds: float = Field(gt=0.0)
    has_audio: bool

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("render_job_id")
    @classmethod
    def validate_render_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("variant_id")
    @classmethod
    def validate_variant_id(cls, value: str | None) -> str | None:
        if value is not None:
            return require_entity_id(value, EntityKind.VARIANT)
        return value


class PublishTarget(FrozenModel):
    """Credential-free destination identity. Secrets remain provider-local."""

    provider_id: str = Field(min_length=1, max_length=128)
    destination_id: str = Field(min_length=1, max_length=512)

    @field_validator("provider_id", "destination_id")
    @classmethod
    def reject_blank_identity(cls, value: str, info) -> str:
        return _nonblank(value, label=info.field_name)


class PublishMetadata(FrozenModel):
    """Portable metadata that participates in exact publish approval identity."""

    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=20_000)
    tags: tuple[str, ...] = Field(default=(), max_length=64)
    visibility: PublishVisibility = "private"
    scheduled_for: datetime | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return _nonblank(value, label="publish title")

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_nonblank(value, label="publish tag") for value in values)
        identities = tuple(value.casefold() for value in normalized)
        if len(identities) != len(set(identities)):
            raise ValueError("publish tags must be unique under case-folding")
        return normalized

    @field_validator("scheduled_for")
    @classmethod
    def validate_scheduled_for(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware_utc(value)


class PublishRequest(FrozenModel):
    """One exact, persistable publish intent with no local paths or credentials."""

    artifact: PublishArtifactRef
    target: PublishTarget
    metadata: PublishMetadata


class PublishApproval(FrozenModel):
    """Explicit human approval for exactly one semantic publish request."""

    contract_version: Literal["pr27_publish_contract_v1"] = _PUBLISH_CONTRACT_VERSION
    request_sha256: SHA256
    approved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    note: str | None = Field(default=None, max_length=4096)

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _nonblank(value, label="publish approval note")


class ApprovedPublishRequest(FrozenModel):
    request: PublishRequest
    approval: PublishApproval

    @model_validator(mode="after")
    def approval_matches_request(self):
        expected = semantic_publish_request_digest(self.request)
        if self.approval.request_sha256 != expected:
            raise ValueError("publish approval does not match the exact request")
        return self


class PublishingProviderHealth(FrozenModel):
    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    available: bool
    reason: str | None = Field(default=None, max_length=4096)


class PublishInvocationEvidence(FrozenModel):
    contract_version: Literal["pr27_publish_contract_v1"] = _PUBLISH_CONTRACT_VERSION
    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    request_sha256: SHA256
    output_sha256: SHA256
    destination_id: str = Field(min_length=1, max_length=512)


class PublishResult(FrozenModel):
    """Validated remote result; no provider credentials or secret material are retained."""

    disposition: PublishDisposition
    remote_id: str = Field(min_length=1, max_length=1024)
    remote_url: str | None = Field(default=None, max_length=4096)
    effective_at: datetime
    evidence: PublishInvocationEvidence

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("remote_url")
    @classmethod
    def validate_remote_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None:
            raise ValueError("publish remote URL must be an absolute HTTP(S) URL without userinfo")
        return value


@runtime_checkable
class PublishingProvider(Protocol):
    """Narrow remote-publishing boundary; concrete platform adapters start after PR27."""

    def health(self) -> PublishingProviderHealth: ...

    def publish(self, request: ApprovedPublishRequest, *, media_path: Path) -> PublishResult: ...


def publish_artifact_ref(manifest: RenderArtifactManifest) -> PublishArtifactRef:
    """Pin publish input to a verified final RenderArtifactManifest."""

    if manifest.purpose != "final":
        raise ValueError("only final render artifacts may become publish inputs")
    return PublishArtifactRef(
        project_id=manifest.project_id,
        render_job_id=manifest.job_id,
        profile_id=manifest.profile_id,
        variant_id=manifest.variant_id,
        render_plan_digest=manifest.render_plan_digest,
        output_sha256=manifest.output_sha256,
        bytes_written=manifest.bytes_written,
        width=manifest.width,
        height=manifest.height,
        duration_seconds=manifest.duration_seconds,
        has_audio=manifest.has_audio,
    )


def semantic_publish_request_digest(request: PublishRequest) -> str:
    """Hash exact machine-independent publish semantics."""

    payload = {
        "contract_version": _PUBLISH_CONTRACT_VERSION,
        "artifact": request.artifact.model_dump(mode="json"),
        "target": request.target.model_dump(mode="json"),
        "metadata": request.metadata.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def approve_publish_request(
    request: PublishRequest,
    *,
    approved_at: datetime | None = None,
    note: str | None = None,
) -> ApprovedPublishRequest:
    approval = PublishApproval(
        request_sha256=semantic_publish_request_digest(request),
        approved_at=datetime.now(timezone.utc) if approved_at is None else approved_at,
        note=note,
    )
    return ApprovedPublishRequest(request=request, approval=approval)


__all__ = [
    "ApprovedPublishRequest",
    "PublishApproval",
    "PublishArtifactRef",
    "PublishDisposition",
    "PublishInvocationEvidence",
    "PublishMetadata",
    "PublishRequest",
    "PublishResult",
    "PublishTarget",
    "PublishVisibility",
    "PublishingExecutionError",
    "PublishingProvider",
    "PublishingProviderError",
    "PublishingProviderHealth",
    "PublishingResponseError",
    "PublishingUnavailableError",
    "approve_publish_request",
    "publish_artifact_ref",
    "semantic_publish_request_digest",
]
