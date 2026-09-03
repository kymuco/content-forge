from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.providers.analytics import (
    AnalyticsExecutionError,
    AnalyticsQuery,
    AnalyticsResponseError,
    AnalyticsWindow,
    SuccessfulPublicationRef,
)
from content_forge.providers.youtube_analytics import (
    YOUTUBE_ANALYTICS_OAUTH_SCOPES,
    YouTubeAnalyticsConfig,
    YouTubeAnalyticsProvider,
)
from content_forge.providers.youtube import YOUTUBE_OAUTH_SCOPES


UTC = timezone.utc


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self, *, num_retries: int):
        assert 0 <= num_retries <= 10
        return self.payload


class _Channels:
    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Request({"items": [{"id": self.channel_id}]})


class _DataService:
    def __init__(self, channel_id: str):
        self._channels = _Channels(channel_id)

    def channels(self):
        return self._channels


class _Reports:
    def __init__(self, daily, aggregate):
        self.daily = daily
        self.aggregate = aggregate
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return _Request(self.daily if kwargs.get("dimensions") == "day" else self.aggregate)


class _AnalyticsService:
    def __init__(self, daily, aggregate):
        self._reports = _Reports(daily, aggregate)

    def reports(self):
        return self._reports


def _publication(*, channel_id: str = "UC_exact_channel", remote_id: str = "AbCdEfGh123"):
    return SuccessfulPublicationRef(
        publish_attempt_id="cf_publish_" + "1" * 32,
        request_sha256="2" * 64,
        project_id="cf_project_" + "3" * 32,
        render_job_id="cf_job_" + "4" * 32,
        output_sha256="5" * 64,
        publication_provider_id="youtube",
        destination_id=channel_id,
        remote_id=remote_id,
        disposition="published",
        effective_at=datetime(2026, 1, 14, 20, tzinfo=UTC),
    )


def _query(*metric_ids: str, start=None, end=None, channel_id: str = "UC_exact_channel"):
    return AnalyticsQuery(
        publication=_publication(channel_id=channel_id),
        window=AnalyticsWindow(
            start_at=start or datetime(2026, 1, 15, 8, tzinfo=UTC),
            end_at=end or datetime(2026, 1, 17, 8, tzinfo=UTC),
        ),
        metric_ids=metric_ids,
    )


def _payloads(api_metrics: tuple[str, ...], values: list[object]):
    daily = {
        "columnHeaders": [{"name": "day"}] + [{"name": name} for name in api_metrics],
        "rows": [
            ["2026-01-15", *values],
            ["2026-01-16", *values],
        ],
    }
    aggregate = {
        "columnHeaders": [{"name": name} for name in api_metrics],
        "rows": [values],
    }
    return daily, aggregate


def _provider(daily, aggregate, *, channel_id: str = "UC_exact_channel", now=None):
    analytics = _AnalyticsService(daily, aggregate)
    data = _DataService(channel_id)
    provider = YouTubeAnalyticsProvider(
        YouTubeAnalyticsConfig(
            token_path="/tmp/content-forge-youtube-analytics-token.json",
            channel_id="UC_exact_channel",
        ),
        credentials_loader=lambda _path: object(),
        analytics_service_factory=lambda _credentials: analytics,
        data_service_factory=lambda _credentials: data,
        clock=lambda: now or datetime(2026, 1, 20, 12, tzinfo=UTC),
    )
    return provider, analytics, data


def test_pr37_scopes_are_read_only_and_do_not_widen_publishing_token_contract():
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in YOUTUBE_ANALYTICS_OAUTH_SCOPES
    assert "https://www.googleapis.com/auth/youtube.readonly" in YOUTUBE_ANALYTICS_OAUTH_SCOPES
    assert "https://www.googleapis.com/auth/youtube.upload" not in YOUTUBE_ANALYTICS_OAUTH_SCOPES
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" not in YOUTUBE_OAUTH_SCOPES


def test_pr37_health_binds_exact_authenticated_channel_and_reuses_same_credentials():
    daily, aggregate = _payloads(("views",), [7])
    provider, _analytics, data = _provider(daily, aggregate)

    health = provider.health()

    assert health.available is True
    assert health.provider_id == "youtube-analytics"
    assert data._channels.calls == [{"part": "id", "mine": True, "maxResults": 2}]


def test_pr37_collects_exact_video_metrics_and_normalizes_watch_minutes_to_seconds():
    # AnalyticsQuery canonicalizes metric IDs, so the provider's exact API request order is
    # likes -> views -> watch_time_seconds/estimatedMinutesWatched.
    api_metrics = ("likes", "views", "estimatedMinutesWatched")
    daily, aggregate = _payloads(api_metrics, [3, 17, 2.5])
    provider, analytics, _data = _provider(daily, aggregate)
    query = _query("views", "watch_time_seconds", "likes")

    assert provider.health().available is True
    observation = provider.observe(query)

    assert observation.availability == "complete"
    assert observation.missing_metric_ids == ()
    values = {item.metric_id: item.value for item in observation.metrics}
    assert values == {"likes": 3, "views": 17, "watch_time_seconds": 150.0}
    assert observation.evidence.provider_id == "youtube-analytics"
    assert observation.evidence.publication_remote_id == "AbCdEfGh123"

    calls = analytics._reports.calls
    assert len(calls) == 2
    for call in calls:
        assert call["ids"] == "channel==MINE"
        assert call["filters"] == "video==AbCdEfGh123"
        assert call["startDate"] == "2026-01-15"
        assert call["endDate"] == "2026-01-16"
        assert call["metrics"] == "likes,views,estimatedMinutesWatched"
    assert calls[0]["dimensions"] == "day"
    assert calls[0]["sort"] == "day"
    assert "dimensions" not in calls[1]


def test_pr37_unknown_requested_metric_is_explicit_partial_not_zero():
    daily, aggregate = _payloads(("views",), [8])
    provider, _analytics, _data = _provider(daily, aggregate)
    query = _query("views", "retention_curve")

    assert provider.health().available is True
    observation = provider.observe(query)

    assert observation.availability == "partial"
    assert {item.metric_id: item.value for item in observation.metrics} == {"views": 8}
    assert observation.missing_metric_ids == ("retention_curve",)


def test_pr37_unsupported_metric_set_returns_unavailable_without_remote_report_call():
    daily, aggregate = _payloads(("views",), [8])
    provider, analytics, _data = _provider(daily, aggregate)
    query = _query("retention_curve")

    assert provider.health().available is True
    observation = provider.observe(query)

    assert observation.availability == "unavailable"
    assert observation.metrics == ()
    assert observation.missing_metric_ids == ("retention_curve",)
    assert observation.unavailable_reason == "youtube_metric_set_unsupported"
    assert analytics._reports.calls == []


def test_pr37_no_rows_is_unavailable_and_never_synthesized_as_zero():
    daily = {"columnHeaders": [{"name": "day"}, {"name": "views"}]}
    aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[0]]}
    provider, analytics, _data = _provider(daily, aggregate)

    assert provider.health().available is True
    observation = provider.observe(_query("views"))

    assert observation.availability == "unavailable"
    assert observation.metrics == ()
    assert observation.missing_metric_ids == ("views",)
    assert observation.unavailable_reason == "youtube_reporting_window_no_data"
    assert len(analytics._reports.calls) == 1


def test_pr37_late_reporting_window_is_unavailable_before_aggregate_query():
    daily = {
        "columnHeaders": [{"name": "day"}, {"name": "views"}],
        "rows": [["2026-01-15", 4]],
    }
    aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[4]]}
    provider, analytics, _data = _provider(daily, aggregate)

    assert provider.health().available is True
    observation = provider.observe(_query("views"))

    assert observation.availability == "unavailable"
    assert observation.unavailable_reason == "youtube_reporting_window_incomplete"
    assert len(analytics._reports.calls) == 1


def test_pr37_future_or_open_window_is_explicitly_unavailable_without_api_call():
    daily, aggregate = _payloads(("views",), [1])
    provider, analytics, _data = _provider(
        daily,
        aggregate,
        now=datetime(2026, 1, 16, 12, tzinfo=UTC),
    )

    assert provider.health().available is True
    observation = provider.observe(_query("views"))

    assert observation.availability == "unavailable"
    assert observation.unavailable_reason == "youtube_reporting_window_not_closed"
    assert analytics._reports.calls == []


def test_pr37_requires_exact_pacific_reporting_day_boundaries():
    daily, aggregate = _payloads(("views",), [1])
    provider, _analytics, _data = _provider(daily, aggregate)
    query = _query(
        "views",
        start=datetime(2026, 1, 15, 9, tzinfo=UTC),
        end=datetime(2026, 1, 17, 9, tzinfo=UTC),
    )

    assert provider.health().available is True
    with pytest.raises(AnalyticsExecutionError, match="Pacific reporting-day"):
        provider.observe(query)


def test_pr37_pacific_alignment_is_dst_aware_not_fixed_utc_offset():
    daily = {
        "columnHeaders": [{"name": "day"}, {"name": "views"}],
        "rows": [["2026-03-08", 2], ["2026-03-09", 2]],
    }
    aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[4]]}
    provider, analytics, _data = _provider(
        daily,
        aggregate,
        now=datetime(2026, 3, 12, 12, tzinfo=UTC),
    )
    query = _query(
        "views",
        start=datetime(2026, 3, 8, 8, tzinfo=UTC),
        end=datetime(2026, 3, 10, 7, tzinfo=UTC),
    )

    assert provider.health().available is True
    observation = provider.observe(query)

    assert observation.availability == "complete"
    assert analytics._reports.calls[0]["startDate"] == "2026-03-08"
    assert analytics._reports.calls[0]["endDate"] == "2026-03-09"


def test_pr37_fails_closed_on_subject_channel_or_publication_provider_mismatch():
    daily, aggregate = _payloads(("views",), [1])
    provider, _analytics, _data = _provider(daily, aggregate)
    assert provider.health().available is True
    with pytest.raises(AnalyticsResponseError, match="channel"):
        provider.observe(_query("views", channel_id="UC_other_channel"))

    bad_publication = _publication().model_copy(update={"publication_provider_id": "other"})
    query = _query("views").model_copy(update={"publication": bad_publication})
    assert provider.health().available is True
    with pytest.raises(AnalyticsExecutionError, match="not a YouTube publication"):
        provider.observe(query)


def test_pr37_observe_requires_same_thread_successful_health_boundary():
    daily, aggregate = _payloads(("views",), [1])
    provider, _analytics, _data = _provider(daily, aggregate)

    with pytest.raises(Exception, match="successful provider health check"):
        provider.observe(_query("views"))
