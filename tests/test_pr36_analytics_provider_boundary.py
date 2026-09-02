from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from content_forge.application.analytics import AnalyticsPublicationError, AnalyticsService
from content_forge.core import EntityKind, new_entity_id
from content_forge.providers import (
    AnalyticsInvocationEvidence,
    AnalyticsMetric,
    AnalyticsObservationBatch,
    AnalyticsProviderHealth,
    AnalyticsQuery,
    AnalyticsResponseError,
    AnalyticsUnavailableError,
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
    semantic_analytics_observation_digest,
    semantic_analytics_query_digest,
    semantic_publish_request_digest,
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
            render_plan_digest=_digest("1"),
            output_sha256=_digest("2"),
            bytes_written=1024,
            width=1080,
            height=1920,
            duration_seconds=12.5,
            has_audio=True,
        ),
        target=PublishTarget(provider_id="fake-publisher", destination_id="channel-123"),
        metadata=PublishMetadata(title="Observed video", visibility="public"),
    )
    approved = approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    prepared = library.publishing.prepare_attempt(approved)
    health = PublishingProviderHealth(
        provider_id="fake-publisher",
        provider_version="1.0",
        available=True,
    )
    library.publishing.mark_running(prepared.attempt_id, health)
    result = PublishResult(
        disposition="published",
        remote_id="video-abc",
        remote_url="https://example.test/video-abc",
        effective_at=datetime(2026, 9, 1, 12, 5, tzinfo=UTC),
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
    return succeeded, request


def _query(library: LocalLibrary, attempt_id: str) -> AnalyticsQuery:
    publication = library.analytics.successful_publication(attempt_id)
    return AnalyticsQuery(
        publication=publication,
        window=AnalyticsWindow(
            start_at=datetime(2026, 9, 1, 12, 5, tzinfo=UTC),
            end_at=datetime(2026, 9, 2, 12, 5, tzinfo=UTC),
        ),
        metric_ids=("views", "average_view_ratio"),
    )


def _complete_observation(query: AnalyticsQuery, *, observed_at: datetime):
    return AnalyticsObservationBatch(
        query=query,
        observed_at=observed_at,
        availability="complete",
        metrics=(
            AnalyticsMetric(metric_id="average_view_ratio", unit="ratio", value=0.42),
            AnalyticsMetric(metric_id="views", unit="count", value=100),
        ),
        evidence=AnalyticsInvocationEvidence(
            provider_id="fake-analytics",
            provider_version="2026.09",
            query_sha256=semantic_analytics_query_digest(query),
            publication_remote_id=query.publication.remote_id,
            provider_observation_id="snapshot-001",
        ),
    )


def test_analytics_query_and_coverage_are_canonical(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    attempt, _request = _successful_publication(library)
    query = AnalyticsQuery(
        publication=library.analytics.successful_publication(attempt.attempt_id),
        window=AnalyticsWindow(
            start_at=datetime(2026, 9, 1, 12, 5, tzinfo=UTC),
            end_at=datetime(2026, 9, 2, 12, 5, tzinfo=UTC),
        ),
        metric_ids=("views", "average_view_ratio"),
    )
    assert query.metric_ids == ("average_view_ratio", "views")

    partial = AnalyticsObservationBatch(
        query=query,
        observed_at=datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
        availability="partial",
        metrics=(AnalyticsMetric(metric_id="views", unit="count", value=10),),
        missing_metric_ids=("average_view_ratio",),
        evidence=AnalyticsInvocationEvidence(
            provider_id="fake-analytics",
            provider_version="1",
            query_sha256=semantic_analytics_query_digest(query),
            publication_remote_id="video-abc",
        ),
    )
    assert partial.missing_metric_ids == ("average_view_ratio",)

    unavailable = AnalyticsObservationBatch(
        query=query,
        observed_at=datetime(2026, 9, 2, 13, 1, tzinfo=UTC),
        availability="unavailable",
        missing_metric_ids=query.metric_ids,
        unavailable_reason="provider has not exposed this reporting window yet",
        evidence=AnalyticsInvocationEvidence(
            provider_id="fake-analytics",
            provider_version="1",
            query_sha256=semantic_analytics_query_digest(query),
            publication_remote_id="video-abc",
        ),
    )
    assert unavailable.metrics == ()

    with pytest.raises(ValidationError):
        AnalyticsObservationBatch(
            query=query,
            observed_at=datetime(2026, 9, 2, 13, 2, tzinfo=UTC),
            availability="complete",
            metrics=(AnalyticsMetric(metric_id="views", unit="count", value=0),),
            evidence=AnalyticsInvocationEvidence(
                provider_id="fake-analytics",
                provider_version="1",
                query_sha256=semantic_analytics_query_digest(query),
                publication_remote_id="video-abc",
            ),
        )


def test_analytics_metrics_keep_zero_distinct_from_unavailable() -> None:
    zero = AnalyticsMetric(metric_id="views", unit="count", value=0)
    assert zero.value == 0
    with pytest.raises(ValidationError):
        AnalyticsMetric(metric_id="views", unit="count", value=0.5)
    with pytest.raises(ValidationError):
        AnalyticsMetric(metric_id="retention", unit="ratio", value=1.01)
    with pytest.raises(ValidationError):
        AnalyticsMetric(metric_id="views", unit="count", value="0")


def test_repository_accepts_only_exact_durable_success_and_is_append_only(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    attempt, _request = _successful_publication(library)
    query = _query(library, attempt.attempt_id)
    first_batch = _complete_observation(
        query,
        observed_at=datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
    )
    first = library.analytics.record_observation(first_batch)
    replay = library.analytics.record_observation(first_batch)
    assert replay == first
    assert replay.ingested_at == first.ingested_at
    assert first.observation_sha256 == semantic_analytics_observation_digest(first_batch)

    later_batch = first_batch.model_copy(
        update={"observed_at": first_batch.observed_at + timedelta(hours=1)}
    )
    later = library.analytics.record_observation(later_batch)
    assert later.observation_sha256 != first.observation_sha256
    history = library.analytics.observations_for_publication(attempt.attempt_id)
    assert [item.observation_sha256 for item in history] == [
        first.observation_sha256,
        later.observation_sha256,
    ]

    wrong_publication = query.publication.model_copy(update={"remote_id": "other-video"})
    wrong_query = query.model_copy(update={"publication": wrong_publication})
    wrong_batch = AnalyticsObservationBatch(
        query=wrong_query,
        observed_at=datetime(2026, 9, 2, 15, 0, tzinfo=UTC),
        availability="complete",
        metrics=(
            AnalyticsMetric(metric_id="average_view_ratio", unit="ratio", value=0.4),
            AnalyticsMetric(metric_id="views", unit="count", value=120),
        ),
        evidence=AnalyticsInvocationEvidence(
            provider_id="fake-analytics",
            provider_version="2026.09",
            query_sha256=semantic_analytics_query_digest(wrong_query),
            publication_remote_id="other-video",
        ),
    )
    with pytest.raises(StorageConflictError):
        library.analytics.record_observation(wrong_batch)


def test_repository_rejects_non_succeeded_publish_attempt(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    request = PublishRequest(
        artifact=PublishArtifactRef(
            project_id=new_entity_id(EntityKind.PROJECT),
            render_job_id=new_entity_id(EntityKind.JOB),
            profile_id="shorts.final",
            render_plan_digest=_digest("3"),
            output_sha256=_digest("4"),
            bytes_written=10,
            width=1080,
            height=1920,
            duration_seconds=1.0,
            has_audio=False,
        ),
        target=PublishTarget(provider_id="fake-publisher", destination_id="channel-456"),
        metadata=PublishMetadata(title="Prepared only"),
    )
    prepared = library.publishing.prepare_attempt(approve_publish_request(request))
    with pytest.raises(StorageConflictError):
        library.analytics.successful_publication(prepared.attempt_id)


class _FakeAnalyticsProvider:
    def __init__(self, *, mismatched_version: bool = False) -> None:
        self.mismatched_version = mismatched_version
        self.calls: list[AnalyticsQuery] = []

    def health(self) -> AnalyticsProviderHealth:
        return AnalyticsProviderHealth(
            provider_id="fake-analytics",
            provider_version="1.0",
            available=True,
        )

    def observe(self, query: AnalyticsQuery) -> AnalyticsObservationBatch:
        self.calls.append(query)
        return AnalyticsObservationBatch(
            query=query,
            observed_at=datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
            availability="complete",
            metrics=tuple(
                AnalyticsMetric(
                    metric_id=metric_id,
                    unit="count" if metric_id == "views" else "ratio",
                    value=5 if metric_id == "views" else 0.5,
                )
                for metric_id in query.metric_ids
            ),
            evidence=AnalyticsInvocationEvidence(
                provider_id="fake-analytics",
                provider_version="2.0" if self.mismatched_version else "1.0",
                query_sha256=semantic_analytics_query_digest(query),
                publication_remote_id=query.publication.remote_id,
            ),
        )


def test_service_collects_through_provider_and_records_history(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    attempt, _request = _successful_publication(library)
    provider = _FakeAnalyticsProvider()
    service = AnalyticsService(library, provider)
    record = service.collect(
        attempt.attempt_id,
        window=AnalyticsWindow(
            start_at=datetime(2026, 9, 1, 12, 5, tzinfo=UTC),
            end_at=datetime(2026, 9, 2, 12, 5, tzinfo=UTC),
        ),
        metric_ids=("views", "average_view_ratio"),
    )
    assert len(provider.calls) == 1
    assert record.observation.query.publication.publish_attempt_id == attempt.attempt_id
    assert service.history(attempt.attempt_id) == (record,)


def test_service_is_provider_optional_and_fails_closed_on_identity_drift(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    attempt, _request = _successful_publication(library)
    window = AnalyticsWindow(
        start_at=datetime(2026, 9, 1, 12, 5, tzinfo=UTC),
        end_at=datetime(2026, 9, 2, 12, 5, tzinfo=UTC),
    )
    with pytest.raises(AnalyticsUnavailableError):
        AnalyticsService(library).collect(
            attempt.attempt_id,
            window=window,
            metric_ids=("views",),
        )

    with pytest.raises(AnalyticsResponseError):
        AnalyticsService(library, _FakeAnalyticsProvider(mismatched_version=True)).collect(
            attempt.attempt_id,
            window=window,
            metric_ids=("views",),
        )
    assert library.analytics.observations_for_publication(attempt.attempt_id) == ()


def test_service_rejects_unknown_publication_before_provider_call(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    provider = _FakeAnalyticsProvider()
    with pytest.raises(AnalyticsPublicationError):
        AnalyticsService(library, provider).collect(
            new_entity_id(EntityKind.PUBLISH),
            window=AnalyticsWindow(
                start_at=datetime(2026, 9, 1, tzinfo=UTC),
                end_at=datetime(2026, 9, 2, tzinfo=UTC),
            ),
            metric_ids=("views",),
        )
    assert provider.calls == []
