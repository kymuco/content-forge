"""PR17 batch preparation, QC, and reproducibility contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal, Mapping, Self

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from content_forge.core import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel
from content_forge.orchestration import RenderPurpose, RenderSourceFingerprint
from content_forge.variants import LocalizedVariantSnapshot

BATCH_MANIFEST_VERSION = "1.0"
BATCH_RESULT_VERSION = "1.0"
QC_REPORT_VERSION = "1.0"
EXPORT_SIDECAR_VERSION = "1.0"

BatchManifestVersion = Literal["1.0"]
BatchResultVersion = Literal["1.0"]
QCReportVersion = Literal["1.0"]
ExportSidecarVersion = Literal["1.0"]
QCStatus = Literal["pass", "fail", "not_evaluable"]
BatchResultStatus = Literal["succeeded", "failed"]
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
ItemKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def canonical_digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[attr-defined]
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProviderParameterSnapshot(FrozenModel):
    """Accepted provider evidence copied from the exact resolved review suggestion."""

    provider: str = Field(min_length=1, max_length=512)
    task_type: RegistryKey
    review_task_id: str
    suggestion_id: str
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)

    @field_validator("review_task_id")
    @classmethod
    def validate_review_task_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.REVIEW)

    @field_validator("suggestion_id")
    @classmethod
    def validate_suggestion_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.SUGGESTION)


class AcceptedStateSnapshot(FrozenModel):
    """Accepted text and provider evidence frozen when a batch is prepared."""

    rendered_text: Mapping[str, str] = Field(default_factory=dict)
    localized_metadata: Mapping[str, JsonValue] = Field(default_factory=dict)
    review_acceptances: Mapping[str, JsonValue] = Field(default_factory=dict)
    provider_parameters: tuple[ProviderParameterSnapshot, ...] = ()

    @model_validator(mode="after")
    def validate_bounds_and_identity(self) -> Self:
        if len(self.rendered_text) > 512:
            raise ValueError("accepted rendered text exceeds supported item count")
        if len(self.localized_metadata) > 256:
            raise ValueError("accepted localized metadata exceeds supported item count")
        if len(self.review_acceptances) > 256:
            raise ValueError("accepted review values exceed supported item count")
        if any(len(key) > 256 or len(value) > 30000 for key, value in self.rendered_text.items()):
            raise ValueError("accepted rendered text exceeds supported bounds")
        ids = [item.suggestion_id for item in self.provider_parameters]
        if len(ids) != len(set(ids)):
            raise ValueError("accepted provider suggestion IDs must be unique")
        return self


class BatchItemSnapshot(FrozenModel):
    """One immutable batch item bound to one initial PR7 render attempt."""

    item_key: ItemKey
    project_id: str
    purpose: RenderPurpose
    profile_id: RegistryKey
    variant_id: str | None = None
    variant_language: str | None = Field(default=None, min_length=2, max_length=35)
    template_id: RegistryKey | None = None
    template_version: str | None = Field(default=None, min_length=1, max_length=64)
    render_plan_digest: Digest
    source_assets: tuple[RenderSourceFingerprint, ...] = ()
    localized_variant: LocalizedVariantSnapshot | None = None
    accepted_state: AcceptedStateSnapshot
    initial_job_id: str

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("variant_id")
    @classmethod
    def validate_variant_id(cls, value: str | None) -> str | None:
        return None if value is None else require_entity_id(value, EntityKind.VARIANT)

    @field_validator("initial_job_id")
    @classmethod
    def validate_initial_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @model_validator(mode="after")
    def validate_identity_pairs(self) -> Self:
        if (self.variant_id is None) != (self.variant_language is None):
            raise ValueError("batch item variant ID/language must be present together")
        if (self.template_id is None) != (self.template_version is None):
            raise ValueError("batch item template ID/version must be present together")
        if self.localized_variant is not None:
            if self.variant_id is None:
                raise ValueError("localized variant snapshot requires a variant identity")
            if (
                self.localized_variant.variant_id,
                self.localized_variant.language,
            ) != (self.variant_id, self.variant_language):
                raise ValueError("localized variant snapshot does not match batch item identity")
        asset_ids = [item.asset_id for item in self.source_assets]
        if asset_ids != sorted(asset_ids) or len(asset_ids) != len(set(asset_ids)):
            raise ValueError("batch item source assets must be unique and sorted by asset ID")
        return self


class BatchManifest(FrozenModel):
    """Immutable batch intent written before the batch becomes runnable."""

    batch_manifest_version: BatchManifestVersion = BATCH_MANIFEST_VERSION
    batch_job_id: str
    items: tuple[BatchItemSnapshot, ...] = Field(min_length=1, max_length=10000)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("batch_job_id")
    @classmethod
    def validate_batch_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_items(self) -> Self:
        keys = [item.item_key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("batch item keys must be unique")
        job_ids = [item.initial_job_id for item in self.items]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("batch initial render job IDs must be unique")
        return self


class QCCheckResult(FrozenModel):
    name: RegistryKey
    status: QCStatus
    blocking: bool = True
    message: str = Field(min_length=1, max_length=4096)
    details: Mapping[str, JsonValue] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.blocking or self.status == "pass"


class RenderQCReport(FrozenModel):
    qc_report_version: QCReportVersion = QC_REPORT_VERSION
    batch_job_id: str
    item_key: ItemKey
    render_job_id: str
    project_id: str
    checks: tuple[QCCheckResult, ...] = Field(min_length=1, max_length=64)
    generated_at: datetime = Field(default_factory=utc_now)

    @field_validator("batch_job_id", "render_job_id")
    @classmethod
    def validate_job_ids(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def unique_check_names(self) -> Self:
        names = [item.name for item in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("QC check names must be unique")
        return self

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


class ExportSidecar(FrozenModel):
    """Portable item-level reproducibility/export record after render + QC."""

    export_sidecar_version: ExportSidecarVersion = EXPORT_SIDECAR_VERSION
    batch_job_id: str
    item_key: ItemKey
    render_job_id: str
    project_id: str
    purpose: RenderPurpose
    profile_id: RegistryKey
    variant_id: str | None = None
    variant_language: str | None = None
    template_id: RegistryKey | None = None
    template_version: str | None = None
    render_plan_digest: Digest
    source_assets: tuple[RenderSourceFingerprint, ...]
    accepted_state: AcceptedStateSnapshot
    localized_variant_digest: Digest | None = None
    renderer_backend_version: str = Field(min_length=1, max_length=64)
    ffmpeg_version: str = Field(min_length=1, max_length=4096)
    video_encoder: str = Field(min_length=1, max_length=128)
    command_manifest_digest: Digest
    output_sha256: Digest
    output_storage_key: str = Field(min_length=1, max_length=1024)
    artifact_manifest_storage_key: str = Field(min_length=1, max_length=1024)
    qc_report: RenderQCReport
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("batch_job_id", "render_job_id")
    @classmethod
    def validate_job_ids(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("variant_id")
    @classmethod
    def validate_variant_id(cls, value: str | None) -> str | None:
        return None if value is None else require_entity_id(value, EntityKind.VARIANT)

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_pairs_and_qc(self) -> Self:
        if (self.variant_id is None) != (self.variant_language is None):
            raise ValueError("export variant ID/language must be present together")
        if (self.template_id is None) != (self.template_version is None):
            raise ValueError("export template ID/version must be present together")
        if self.qc_report.batch_job_id != self.batch_job_id:
            raise ValueError("export QC batch identity does not match")
        if self.qc_report.render_job_id != self.render_job_id:
            raise ValueError("export QC render identity does not match")
        if self.qc_report.item_key != self.item_key:
            raise ValueError("export QC item identity does not match")
        return self


class BatchItemResult(FrozenModel):
    item_key: ItemKey
    render_job_id: str
    attempt_index: int = Field(ge=0)
    state: Literal["succeeded", "failed"]
    qc_passed: bool | None = None
    export_sidecar_storage_key: str | None = Field(default=None, max_length=1024)
    export_sidecar_digest: Digest | None = None
    failure_code: str | None = Field(default=None, max_length=128)
    failure_message: str | None = Field(default=None, max_length=4096)

    @field_validator("render_job_id")
    @classmethod
    def validate_render_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        has_export = self.export_sidecar_storage_key is not None
        if has_export != (self.export_sidecar_digest is not None):
            raise ValueError("batch item export key/digest must be present together")
        if self.state == "succeeded":
            if not has_export or self.qc_passed is not True:
                raise ValueError("successful batch item requires a passing export sidecar")
            if self.failure_code is not None or self.failure_message is not None:
                raise ValueError("successful batch item cannot carry failure details")
        else:
            if self.failure_code is None or self.failure_message is None:
                raise ValueError("failed batch item requires failure details")
        return self


class BatchResultManifest(FrozenModel):
    batch_result_version: BatchResultVersion = BATCH_RESULT_VERSION
    batch_job_id: str
    status: BatchResultStatus
    items: tuple[BatchItemResult, ...]
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("batch_job_id")
    @classmethod
    def validate_batch_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        keys = [item.item_key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("batch result item keys must be unique")
        expected = "succeeded" if all(item.state == "succeeded" for item in self.items) else "failed"
        if self.status != expected:
            raise ValueError("batch result status does not match item states")
        return self


__all__ = [
    "AcceptedStateSnapshot",
    "BATCH_MANIFEST_VERSION",
    "BATCH_RESULT_VERSION",
    "BatchItemResult",
    "BatchItemSnapshot",
    "BatchManifest",
    "BatchResultManifest",
    "EXPORT_SIDECAR_VERSION",
    "ExportSidecar",
    "ProviderParameterSnapshot",
    "QCCheckResult",
    "QC_REPORT_VERSION",
    "RenderQCReport",
    "canonical_digest",
]
