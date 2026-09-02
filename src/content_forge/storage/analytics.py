"""PR36 append-only analytics observations linked to durable successful publications."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import field_validator, model_validator

from content_forge.core import EntityKind, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.providers.analytics import (
    AnalyticsObservationBatch,
    SuccessfulPublicationRef,
    semantic_analytics_observation_digest,
    semantic_analytics_query_digest,
)
from content_forge.providers.publishing import ApprovedPublishRequest
from content_forge.providers.publishing_validation import validate_publish_result

from .database import LibraryDatabase, StorageConflictError, StorageSchemaError
from .publishing import PublishingRepository

_ANALYTICS_SCHEMA_COMPONENT = "analytics"
_ANALYTICS_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(model) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class AnalyticsObservationRecord(FrozenModel):
    """Stored observation plus local ingestion time, which is not observation time."""

    observation_sha256: SHA256
    observation: AnalyticsObservationBatch
    ingested_at: datetime

    @field_validator("ingested_at")
    @classmethod
    def validate_ingested_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_identity(self):
        if self.observation_sha256 != semantic_analytics_observation_digest(self.observation):
            raise ValueError("analytics observation digest mismatch")
        return self


class AnalyticsRepository:
    """History-preserving analytics storage over the durable publishing ledger."""

    def __init__(self, database: LibraryDatabase, publishing: PublishingRepository) -> None:
        self.database = database
        self.publishing = publishing

    def initialize(self) -> "AnalyticsRepository":
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM application_schema WHERE component = ?",
                (_ANALYTICS_SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > _ANALYTICS_SCHEMA_VERSION:
                raise StorageSchemaError(
                    f"analytics schema {version} is newer than supported {_ANALYTICS_SCHEMA_VERSION}"
                )
            if version not in {0, _ANALYTICS_SCHEMA_VERSION}:
                raise StorageSchemaError(
                    "unsupported analytics schema migration: "
                    f"{version} -> {_ANALYTICS_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS analytics_observations (
                    observation_sha256 TEXT PRIMARY KEY,
                    publish_attempt_id TEXT NOT NULL REFERENCES publish_attempts(attempt_id),
                    request_sha256 TEXT NOT NULL,
                    query_sha256 TEXT NOT NULL,
                    analytics_provider_id TEXT NOT NULL,
                    analytics_provider_version TEXT NOT NULL,
                    publication_remote_id TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    observation_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_observations_publication
                ON analytics_observations(publish_attempt_id, observed_at, ingested_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_observations_window
                ON analytics_observations(publish_attempt_id, window_start, window_end, observed_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_observations_provider
                ON analytics_observations(analytics_provider_id, analytics_provider_version)
                """
            )
            if version == 0:
                connection.execute(
                    "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                    (_ANALYTICS_SCHEMA_COMPONENT, _ANALYTICS_SCHEMA_VERSION),
                )
        return self

    def successful_publication(self, publish_attempt_id: str) -> SuccessfulPublicationRef:
        """Re-authenticate one durable success before it becomes an analytics subject."""

        publish_attempt_id = require_entity_id(publish_attempt_id, EntityKind.PUBLISH)
        attempt = self.publishing.get_attempt(publish_attempt_id)
        if attempt is None:
            raise StorageConflictError("analytics subject publish attempt does not exist")
        if attempt.state != "succeeded" or attempt.result is None:
            raise StorageConflictError("analytics subject must be a succeeded publish attempt")
        if attempt.provider_health is None:
            raise StorageConflictError("successful publish attempt lacks provider health evidence")
        operation = self.publishing.get_operation(attempt.request_sha256)
        if operation is None:
            raise StorageConflictError("successful publish attempt references missing operation")
        try:
            approved = ApprovedPublishRequest(
                request=operation.request,
                approval=attempt.approval,
            )
            validate_publish_result(approved, attempt.provider_health, attempt.result)
        except Exception as exc:
            raise StorageConflictError(
                "successful publication evidence does not match durable approved request"
            ) from exc

        request = operation.request
        result = attempt.result
        return SuccessfulPublicationRef(
            publish_attempt_id=attempt.attempt_id,
            request_sha256=attempt.request_sha256,
            project_id=request.artifact.project_id,
            render_job_id=request.artifact.render_job_id,
            output_sha256=request.artifact.output_sha256,
            publication_provider_id=request.target.provider_id,
            destination_id=request.target.destination_id,
            remote_id=result.remote_id,
            disposition=result.disposition,
            effective_at=result.effective_at,
        )

    @staticmethod
    def _decode(row) -> AnalyticsObservationRecord:
        try:
            observation = AnalyticsObservationBatch.model_validate_json(
                str(row["observation_json"])
            )
            record = AnalyticsObservationRecord(
                observation_sha256=str(row["observation_sha256"]),
                observation=observation,
                ingested_at=datetime.fromisoformat(str(row["ingested_at"])),
            )
        except Exception as exc:
            raise StorageConflictError("stored analytics observation is invalid") from exc

        publication = observation.query.publication
        evidence = observation.evidence
        expected = {
            "publish_attempt_id": publication.publish_attempt_id,
            "request_sha256": publication.request_sha256,
            "query_sha256": semantic_analytics_query_digest(observation.query),
            "analytics_provider_id": evidence.provider_id,
            "analytics_provider_version": evidence.provider_version,
            "publication_remote_id": publication.remote_id,
            "window_start": observation.query.window.start_at.isoformat(),
            "window_end": observation.query.window.end_at.isoformat(),
            "observed_at": observation.observed_at.isoformat(),
        }
        for column, expected_value in expected.items():
            if str(row[column]) != expected_value:
                raise StorageConflictError(
                    f"analytics observation index evidence mismatch: {column}"
                )
        if str(row["query_sha256"]) != evidence.query_sha256:
            raise StorageConflictError("analytics observation query evidence mismatch")
        return record

    @staticmethod
    def _canonical_observation(
        observation: AnalyticsObservationBatch,
    ) -> AnalyticsObservationBatch:
        try:
            canonical = AnalyticsObservationBatch.model_validate(
                observation.model_dump(mode="python")
            )
        except Exception as exc:
            raise StorageConflictError("analytics observation is invalid") from exc
        if canonical != observation:
            raise StorageConflictError("analytics observation is not canonical")
        return canonical

    def record_observation(
        self,
        observation: AnalyticsObservationBatch,
    ) -> AnalyticsObservationRecord:
        observation = self._canonical_observation(observation)
        expected = self.successful_publication(observation.query.publication.publish_attempt_id)
        if observation.query.publication != expected:
            raise StorageConflictError(
                "analytics observation publication identity differs from durable success"
            )

        digest = semantic_analytics_observation_digest(observation)
        encoded = _json(observation)
        now = _now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM analytics_observations WHERE observation_sha256 = ?",
                (digest,),
            ).fetchone()
            if existing is not None:
                record = self._decode(existing)
                if _json(record.observation) != encoded:
                    raise StorageConflictError("analytics observation digest collision")
                return record

            connection.execute(
                """
                INSERT INTO analytics_observations(
                    observation_sha256,
                    publish_attempt_id,
                    request_sha256,
                    query_sha256,
                    analytics_provider_id,
                    analytics_provider_version,
                    publication_remote_id,
                    window_start,
                    window_end,
                    observed_at,
                    ingested_at,
                    observation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    expected.publish_attempt_id,
                    expected.request_sha256,
                    observation.evidence.query_sha256,
                    observation.evidence.provider_id,
                    observation.evidence.provider_version,
                    expected.remote_id,
                    observation.query.window.start_at.isoformat(),
                    observation.query.window.end_at.isoformat(),
                    observation.observed_at.isoformat(),
                    now.isoformat(),
                    encoded,
                ),
            )
        return AnalyticsObservationRecord(
            observation_sha256=digest,
            observation=observation,
            ingested_at=now,
        )

    def get_observation(self, observation_sha256: str) -> AnalyticsObservationRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM analytics_observations WHERE observation_sha256 = ?",
                (observation_sha256,),
            ).fetchone()
        if row is None:
            return None
        record = self._decode(row)
        current = self.successful_publication(
            record.observation.query.publication.publish_attempt_id
        )
        if record.observation.query.publication != current:
            raise StorageConflictError("analytics observation publication evidence is stale")
        return record

    def observations_for_publication(
        self,
        publish_attempt_id: str,
        *,
        limit: int = 256,
    ) -> tuple[AnalyticsObservationRecord, ...]:
        publish_attempt_id = require_entity_id(publish_attempt_id, EntityKind.PUBLISH)
        if limit < 1 or limit > 4096:
            raise ValueError("analytics observation limit must be between 1 and 4096")
        current = self.successful_publication(publish_attempt_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analytics_observations
                WHERE publish_attempt_id = ?
                ORDER BY observed_at, ingested_at, observation_sha256
                LIMIT ?
                """,
                (publish_attempt_id, limit),
            ).fetchall()
        records = tuple(self._decode(row) for row in rows)
        if any(record.observation.query.publication != current for record in records):
            raise StorageConflictError("analytics history publication evidence is inconsistent")
        return records


__all__ = ["AnalyticsObservationRecord", "AnalyticsRepository"]
