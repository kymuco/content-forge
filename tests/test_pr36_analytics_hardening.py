from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from content_forge.core import EntityKind, new_entity_id
from content_forge.providers import (
    AnalyticsInvocationEvidence,
    AnalyticsMetric,
    AnalyticsObservationBatch,
    AnalyticsProviderHealth,
    AnalyticsQuery,
    AnalyticsResponseError,
    AnalyticsWindow,
    PublishArtifactRef,
    PublishInvocationEvidence,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    PublishTarget,
    PublishingProviderHealth,
    approve_publish_request,
    publish_idempotency_key,
    semantic_analytics_query_digest,
    semantic_publish_request_digest,
    validate_analytics_observation,
)
from content_forge.storage import LocalLibrary, StorageConflictError

UTC = timezone.utc


def _digest(character: str) -> str:
    return character * 64


def _successful_publication(library: LocalLibrary):
    request = PublishRequest(
        artifact=PublishArtifactRef(
            project_id=new_entity_id(EntityKind.PROJECT),
            render_job_id=new_entity_id(EntityKind.JOB),
            profile_id="shorts.final",
            render_plan_digest=_digest("a"),
            output_sha256=_digest("b"),
            bytes_written=2048,
            width=1080,
            height=1920,
            duration_seconds=8.0,
            has_audio=True,
        ),
        target=PublishTarget(provider_id="publisher", destination_id="channel"),
        metadata=PublishMetadata(title="Measured publication", visibility="public"),
    )
    approved = approve_publish_request(request)
    prepared = library.publishing.prepare_attempt(approved)
    health = PublishingProviderHealth(
        provider_id="publisher",
        provider_version="1",
        available=True,
    )
    library.publishing.mark_running(prepared.attempt_id, health)
    effective_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    result = PublishResult(
        disposition="published",
        remote_id="remote-video",
        effective_at=effective_at,
        evidence=PublishInvocationEvidence(
            provider_id=health.provider_id,
            provider_version=health.provider_version,
            request_sha256=semantic_publish_request_digest(request),
            idempotency_key=publish_idempotency_key(request),
            output_sha256=request.artifact.output_sha256,
            destination_id=request.target.destination_id,
        ),
    )
    succeeded = library.publishing.mark_succeeded(prepared.attempt_id, result)
    return succeeded, effective_at


def _valid_observation(library: LocalLibrary, attempt_id: str):
    publication = library.analytics.successful_publication(attempt_id)
    query = AnalyticsQuery(
        publication=publication,
        window=AnalyticsWindow(
            start_at=publication.effective_at,
            end_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        ),
        metric_ids=("views",),
    )
    health = AnalyticsProviderHealth(
        provider_id="analytics",
        provider_version="1",
        available=True,
    )
    observation = AnalyticsObservationBatch(
        query=query,
        observed_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        availability="complete",
        metrics=(AnalyticsMetric(metric_id="views", unit="count", value=1),),
        evidence=AnalyticsInvocationEvidence(
            provider_id=health.provider_id,
            provider_version=health.provider_version,
            query_sha256=semantic_analytics_query_digest(query),
            publication_remote_id=publication.remote_id,
        ),
    )
    return query, health, observation


def test_analytics_window_cannot_begin_before_publication(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    attempt, effective_at = _successful_publication(library)
    publication = library.analytics.successful_publication(attempt.attempt_id)
    with pytest.raises(ValidationError):
        AnalyticsQuery(
            publication=publication,
            window=AnalyticsWindow(
                start_at=datetime(2026, 9, 1, 9, 59, tzinfo=UTC),
                end_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
            ),
            metric_ids=("views",),
        )
    assert publication.effective_at == effective_at


def test_observed_at_may_be_provisional_but_not_predate_window(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    attempt, _effective_at = _successful_publication(library)
    query, _health, observation = _valid_observation(library, attempt.attempt_id)
    assert observation.observed_at < query.window.end_at

    with pytest.raises(ValidationError):
        AnalyticsObservationBatch(
            query=query,
            observed_at=datetime(2026, 9, 1, 9, 59, tzinfo=UTC),
            availability="complete",
            metrics=(AnalyticsMetric(metric_id="views", unit="count", value=1),),
            evidence=AnalyticsInvocationEvidence(
                provider_id="analytics",
                provider_version="1",
                query_sha256=semantic_analytics_query_digest(query),
                publication_remote_id=query.publication.remote_id,
            ),
        )


def test_model_copy_validation_bypass_is_rejected_at_provider_and_storage_boundaries(
    tmp_path,
) -> None:
    library = LocalLibrary(tmp_path)
    attempt, _effective_at = _successful_publication(library)
    query, health, observation = _valid_observation(library, attempt.attempt_id)
    tampered = observation.model_copy(update={"metrics": ()})

    with pytest.raises(AnalyticsResponseError):
        validate_analytics_observation(query, health, tampered)
    with pytest.raises(StorageConflictError):
        library.analytics.record_observation(tampered)
    assert library.analytics.observations_for_publication(attempt.attempt_id) == ()


def test_analytics_schema_is_lazy_and_provider_free_runtime_stays_usable(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    with library.database.connection() as connection:
        before = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analytics_observations'"
        ).fetchone()
    assert before is None

    repository = library.analytics
    assert repository is library.analytics
    with library.database.connection() as connection:
        after = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analytics_observations'"
        ).fetchone()
    assert after is not None


def test_stored_observation_json_is_revalidated_on_read(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    attempt, _effective_at = _successful_publication(library)
    _query, _health, observation = _valid_observation(library, attempt.attempt_id)
    record = library.analytics.record_observation(observation)

    with library.database.transaction() as connection:
        connection.execute(
            "UPDATE analytics_observations SET observation_json = ? WHERE observation_sha256 = ?",
            ("{}", record.observation_sha256),
        )
    with pytest.raises(ValidationError):
        library.analytics.get_observation(record.observation_sha256)
