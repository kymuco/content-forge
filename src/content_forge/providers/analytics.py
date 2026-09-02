"""PR36 platform-agnostic analytics provider and observation contracts."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from content_forge.core import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, SHA256

AnalyticsAvailability = Literal["complete", "partial", "unavailable"]
AnalyticsMetricUnit = Literal["count", "ratio", "seconds", "currency_minor", "score"]
PublishDisposition = Literal["published", "scheduled"]


class AnalyticsProviderError(RuntimeError):
    """Base class for optional analytics-provider failures."""


class AnalyticsUnavailableError(AnalyticsProviderError):
    """The provider runtime/account integration is unavailable."""


class AnalyticsExecutionError(AnalyticsProviderError):
    """The provider failed while retrieving observations."""


class AnalyticsResponseError(AnalyticsProviderError):
    """A provider response violated the analytics result contract."""


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nonblank(value: str, *, label: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label} must contain non-whitespace content")
    return normalized


class SuccessfulPublicationRef(FrozenModel):
    """Exact durable successful-publication identity used as analytics subject."""

    publish_attempt_id: str
    request_sha256: SHA256
    project_id: str
    render_job_id: str
    output_sha256: SHA256
    publication_provider_id: str = Field(min_length=1, max_length=128)
    destination_id: str = Field(min_length=1, max_length=512)
    remote_id: str = Field(min_length=1, max_length=1024)
    disposition: PublishDisposition
    effective_at: datetime

    @field_validator("publish_attempt_id")
    @classmethod
    def validate_publish_attempt_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PUBLISH)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("render_job_id")
    @classmethod
    def validate_render_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("publication_provider_id", "destination_id")
    @classmethod
    def normalize_identity(cls, value: str, info) -> str:
        return _nonblank(value, label=info.field_name)

    @field_validator("remote_id")
    @classmethod
    def validate_remote_id(cls, value: str) -> str:
        if value != value.strip() or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        ):
            raise ValueError("publication remote ID must be a canonical opaque string")
        return value

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class AnalyticsWindow(FrozenModel):
    """Half-open provider observation interval, independent from ingestion time."""

    start_at: datetime
    end_at: datetime

    @field_validator("start_at", "end_at")
    @classmethod
    def validate_datetime(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def validate_order(self):
        if self.end_at <= self.start_at:
            raise ValueError("analytics window end must be after start")
        return self


class AnalyticsMetric(FrozenModel):
    """One normalized metric with explicit unit semantics."""

    metric_id: RegistryKey
    unit: AnalyticsMetricUnit
    value: int | float

    @field_validator("value", mode="before")
    @classmethod
    def reject_non_numeric(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("analytics metric value must be numeric")
        return value

    @model_validator(mode="after")
    def validate_unit_value(self):
        numeric = float(self.value)
        if not math.isfinite(numeric):
            raise ValueError("analytics metric value must be finite")
        if self.unit in {"count", "currency_minor"}:
            if not isinstance(self.value, int) or self.value < 0:
                raise ValueError(f"analytics {self.unit} must be a non-negative integer")
        elif self.unit == "ratio":
            if numeric < 0.0 or numeric > 1.0:
                raise ValueError("analytics ratio must be between 0 and 1")
        elif self.unit == "seconds" and numeric < 0.0:
            raise ValueError("analytics seconds must be non-negative")
        return self


class AnalyticsQuery(FrozenModel):
    """One exact request for observations about one successful publication."""

    publication: SuccessfulPublicationRef
    window: AnalyticsWindow
    metric_ids: tuple[RegistryKey, ...] = Field(min_length=1, max_length=64)

    @field_validator("metric_ids")
    @classmethod
    def canonicalize_metric_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("analytics metric IDs must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def validate_publication_window(self):
        if self.window.start_at < self.publication.effective_at:
            raise ValueError("analytics window cannot begin before publication effective time")
        return self


class AnalyticsProviderHealth(FrozenModel):
    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    available: bool
    reason: str | None = Field(default=None, max_length=4096)

    @field_validator("provider_id", "provider_version")
    @classmethod
    def normalize_identity(cls, value: str, info) -> str:
        return _nonblank(value, label=info.field_name)


class AnalyticsInvocationEvidence(FrozenModel):
    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    query_sha256: SHA256
    publication_remote_id: str = Field(min_length=1, max_length=1024)
    provider_observation_id: str | None = Field(default=None, max_length=1024)

    @field_validator("provider_id", "provider_version")
    @classmethod
    def normalize_identity(cls, value: str, info) -> str:
        return _nonblank(value, label=info.field_name)

    @field_validator("publication_remote_id")
    @classmethod
    def validate_publication_remote_id(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("analytics publication remote ID must be canonical")
        return value

    @field_validator("provider_observation_id")
    @classmethod
    def normalize_provider_observation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        ):
            raise ValueError("provider observation ID must be a canonical opaque string")
        return value


class AnalyticsObservationBatch(FrozenModel):
    """One immutable provider observation; missing data is never encoded as zero."""

    query: AnalyticsQuery
    observed_at: datetime
    availability: AnalyticsAvailability
    metrics: tuple[AnalyticsMetric, ...] = ()
    missing_metric_ids: tuple[RegistryKey, ...] = ()
    unavailable_reason: str | None = Field(default=None, max_length=4096)
    evidence: AnalyticsInvocationEvidence

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("metrics")
    @classmethod
    def unique_metrics(cls, values: tuple[AnalyticsMetric, ...]) -> tuple[AnalyticsMetric, ...]:
        ids = tuple(item.metric_id for item in values)
        if len(ids) != len(set(ids)):
            raise ValueError("analytics observation metrics must be unique")
        return tuple(sorted(values, key=lambda item: item.metric_id))

    @field_validator("missing_metric_ids")
    @classmethod
    def canonicalize_missing(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("missing analytics metric IDs must be unique")
        return tuple(sorted(values))

    @field_validator("unavailable_reason")
    @classmethod
    def normalize_unavailable_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _nonblank(value, label="analytics unavailable reason")

    @model_validator(mode="after")
    def validate_coverage(self):
        requested = set(self.query.metric_ids)
        returned = {item.metric_id for item in self.metrics}
        missing = set(self.missing_metric_ids)
        if self.observed_at < self.query.window.start_at:
            raise ValueError("analytics observation cannot predate requested window")
        if returned & missing:
            raise ValueError("analytics metric cannot be both returned and missing")
        if returned | missing != requested:
            raise ValueError("analytics coverage must exactly partition requested metrics")
        if self.availability == "complete":
            if missing or self.unavailable_reason is not None:
                raise ValueError("complete analytics observation cannot report missing data")
        elif self.availability == "partial":
            if not returned or not missing or self.unavailable_reason is not None:
                raise ValueError("partial analytics observation requires returned and missing metrics")
        else:
            if returned or missing != requested or self.unavailable_reason is None:
                raise ValueError("unavailable analytics observation requires explicit missing coverage")
        if self.evidence.query_sha256 != semantic_analytics_query_digest(self.query):
            raise ValueError("analytics evidence query digest mismatch")
        if self.evidence.publication_remote_id != self.query.publication.remote_id:
            raise ValueError("analytics evidence publication identity mismatch")
        return self


@runtime_checkable
class AnalyticsProvider(Protocol):
    """Replaceable read-only analytics boundary independent from publishing providers."""

    def health(self) -> AnalyticsProviderHealth: ...

    def observe(self, query: AnalyticsQuery) -> AnalyticsObservationBatch: ...


def _canonical_json(model: FrozenModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def semantic_analytics_query_digest(query: AnalyticsQuery) -> str:
    return hashlib.sha256(_canonical_json(query)).hexdigest()


def semantic_analytics_observation_digest(observation: AnalyticsObservationBatch) -> str:
    return hashlib.sha256(_canonical_json(observation)).hexdigest()


def validate_analytics_observation(
    query: AnalyticsQuery,
    health: AnalyticsProviderHealth,
    observation: AnalyticsObservationBatch,
) -> None:
    """Fail closed if provider identity, model validity, or exact query evidence changed."""

    if not health.available:
        raise AnalyticsResponseError("unavailable provider cannot return analytics observations")
    try:
        canonical = AnalyticsObservationBatch.model_validate(
            observation.model_dump(mode="python")
        )
    except Exception as exc:
        raise AnalyticsResponseError("analytics provider returned invalid observation data") from exc
    if canonical != observation:
        raise AnalyticsResponseError("analytics provider response is not canonical")
    if observation.query != query:
        raise AnalyticsResponseError("analytics provider returned a different query")
    evidence = observation.evidence
    if evidence.provider_id != health.provider_id:
        raise AnalyticsResponseError("analytics provider ID changed between health and response")
    if evidence.provider_version != health.provider_version:
        raise AnalyticsResponseError("analytics provider version changed between health and response")
    if evidence.query_sha256 != semantic_analytics_query_digest(query):
        raise AnalyticsResponseError("analytics provider evidence does not match exact query")


__all__ = [
    "AnalyticsAvailability",
    "AnalyticsExecutionError",
    "AnalyticsInvocationEvidence",
    "AnalyticsMetric",
    "AnalyticsMetricUnit",
    "AnalyticsObservationBatch",
    "AnalyticsProvider",
    "AnalyticsProviderError",
    "AnalyticsProviderHealth",
    "AnalyticsQuery",
    "AnalyticsResponseError",
    "AnalyticsUnavailableError",
    "AnalyticsWindow",
    "SuccessfulPublicationRef",
    "semantic_analytics_observation_digest",
    "semantic_analytics_query_digest",
    "validate_analytics_observation",
]
