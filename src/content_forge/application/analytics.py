"""PR36 analytics collection orchestration over successful publication evidence."""

from __future__ import annotations

from content_forge.providers import (
    AnalyticsExecutionError,
    AnalyticsObservationBatch,
    AnalyticsProvider,
    AnalyticsProviderError,
    AnalyticsQuery,
    AnalyticsResponseError,
    AnalyticsUnavailableError,
    AnalyticsWindow,
    validate_analytics_observation,
)
from content_forge.storage import AnalyticsObservationRecord, LocalLibrary


class AnalyticsOrchestrationError(RuntimeError):
    """Base class for local analytics orchestration failures."""


class AnalyticsPublicationError(AnalyticsOrchestrationError):
    """The requested analytics subject is not a durable successful publication."""


class AnalyticsService:
    """Collect observations without granting analytics any production authority."""

    def __init__(
        self,
        library: LocalLibrary,
        provider: AnalyticsProvider | None = None,
    ) -> None:
        self.library = library
        self.provider = provider

    def query(
        self,
        publish_attempt_id: str,
        *,
        window: AnalyticsWindow,
        metric_ids: tuple[str, ...],
    ) -> AnalyticsQuery:
        try:
            publication = self.library.analytics.successful_publication(publish_attempt_id)
        except Exception as exc:
            raise AnalyticsPublicationError(
                "analytics requires an exact durable successful publication"
            ) from exc
        return AnalyticsQuery(
            publication=publication,
            window=window,
            metric_ids=metric_ids,
        )

    def collect(
        self,
        publish_attempt_id: str,
        *,
        window: AnalyticsWindow,
        metric_ids: tuple[str, ...],
    ) -> AnalyticsObservationRecord:
        provider = self.provider
        if provider is None:
            raise AnalyticsUnavailableError("analytics provider is not configured")

        query = self.query(
            publish_attempt_id,
            window=window,
            metric_ids=metric_ids,
        )
        try:
            health = provider.health()
        except AnalyticsProviderError:
            raise
        except Exception as exc:
            raise AnalyticsExecutionError("analytics provider health check failed") from exc
        if not health.available:
            raise AnalyticsUnavailableError("analytics provider is unavailable")

        try:
            observation = provider.observe(query)
        except AnalyticsProviderError:
            raise
        except Exception as exc:
            raise AnalyticsExecutionError("analytics provider observation failed") from exc

        try:
            validate_analytics_observation(query, health, observation)
        except AnalyticsResponseError:
            raise
        except Exception as exc:
            raise AnalyticsResponseError("analytics provider response is invalid") from exc

        return self.library.analytics.record_observation(observation)

    def history(
        self,
        publish_attempt_id: str,
        *,
        limit: int = 256,
    ) -> tuple[AnalyticsObservationRecord, ...]:
        return self.library.analytics.observations_for_publication(
            publish_attempt_id,
            limit=limit,
        )


__all__ = [
    "AnalyticsOrchestrationError",
    "AnalyticsPublicationError",
    "AnalyticsService",
]
