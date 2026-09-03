from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from content_forge.providers.analytics import (
    AnalyticsExecutionError,
    AnalyticsQuery,
    AnalyticsResponseError,
    AnalyticsWindow,
    SuccessfulPublicationRef,
)
from content_forge.providers.youtube_analytics import (
    YouTubeAnalyticsConfig,
    YouTubeAnalyticsProvider,
    _safe_token_file,
)
from content_forge.providers.youtube_analytics_auth import (
    _token_target,
    authorize_youtube_analytics,
)


UTC = timezone.utc


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self, *, num_retries: int):
        return self.payload


class _Reports:
    def __init__(self, daily, aggregate):
        self.daily = daily
        self.aggregate = aggregate
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.daily if kwargs.get("dimensions") == "day" else self.aggregate
        return _Request(payload)


class _AnalyticsService:
    def __init__(self, daily, aggregate):
        self._reports = _Reports(daily, aggregate)

    def reports(self):
        return self._reports


class _Channels:
    def __init__(self, channel_id="UC_exact_channel"):
        self.channel_id = channel_id

    def list(self, **_kwargs):
        return _Request({"items": [{"id": self.channel_id}]})


class _DataService:
    def __init__(self, channel_id="UC_exact_channel"):
        self._channels = _Channels(channel_id)

    def channels(self):
        return self._channels


def _query(*metric_ids: str, remote_id: str = "AbCdEfGh123", start=None, end=None):
    publication = SuccessfulPublicationRef(
        publish_attempt_id="cf_publish_" + "1" * 32,
        request_sha256="2" * 64,
        project_id="cf_project_" + "3" * 32,
        render_job_id="cf_job_" + "4" * 32,
        output_sha256="5" * 64,
        publication_provider_id="youtube",
        destination_id="UC_exact_channel",
        remote_id=remote_id,
        disposition="published",
        effective_at=datetime(2026, 1, 14, 20, tzinfo=UTC),
    )
    return AnalyticsQuery(
        publication=publication,
        window=AnalyticsWindow(
            start_at=start or datetime(2026, 1, 15, 8, tzinfo=UTC),
            end_at=end or datetime(2026, 1, 17, 8, tzinfo=UTC),
        ),
        metric_ids=metric_ids,
    )


def _provider(daily, aggregate, *, max_window_days: int = 366, channel_id="UC_exact_channel"):
    analytics = _AnalyticsService(daily, aggregate)
    provider = YouTubeAnalyticsProvider(
        YouTubeAnalyticsConfig(
            token_path="/tmp/content-forge-youtube-analytics-token.json",
            channel_id="UC_exact_channel",
            max_window_days=max_window_days,
        ),
        credentials_loader=lambda _path: object(),
        analytics_service_factory=lambda _credentials: analytics,
        data_service_factory=lambda _credentials: _DataService(channel_id),
        clock=lambda: datetime(2026, 1, 20, 12, tzinfo=UTC),
    )
    assert provider.health().available is (channel_id == "UC_exact_channel")
    return provider


def _provider_and_service(daily, aggregate):
    analytics = _AnalyticsService(daily, aggregate)
    provider = YouTubeAnalyticsProvider(
        YouTubeAnalyticsConfig(
            token_path="/tmp/content-forge-youtube-analytics-token.json",
            channel_id="UC_exact_channel",
        ),
        credentials_loader=lambda _path: object(),
        analytics_service_factory=lambda _credentials: analytics,
        data_service_factory=lambda _credentials: _DataService(),
        clock=lambda: datetime(2026, 1, 20, 12, tzinfo=UTC),
    )
    assert provider.health().available is True
    return provider, analytics


def _valid_views_payloads():
    daily = {
        "columnHeaders": [{"name": "day"}, {"name": "views"}],
        "rows": [["2026-01-15", 2], ["2026-01-16", 3]],
    }
    aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[5]]}
    return daily, aggregate


def test_pr37_daily_probe_requests_one_row_budget_per_reporting_day():
    daily, aggregate = _valid_views_payloads()
    provider, analytics = _provider_and_service(daily, aggregate)

    observation = provider.observe(_query("views"))

    assert observation.availability == "complete"
    assert analytics._reports.calls[0]["maxResults"] == 2
    assert analytics._reports.calls[0]["dimensions"] == "day"
    assert "maxResults" not in analytics._reports.calls[1]


def test_pr37_embedded_google_errors_fail_closed_even_with_table_shape():
    daily, aggregate = _valid_views_payloads()
    daily["errors"] = [{"code": "lateData", "message": "not complete"}]
    provider = _provider(daily, aggregate)

    with pytest.raises(AnalyticsResponseError, match="embedded error evidence"):
        provider.observe(_query("views"))


def test_pr37_wrong_result_kind_or_start_index_fails_closed():
    aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[5]]}
    for extra, message in (
        ({"kind": "other#table"}, "unexpected result kind"),
        ({"startIndex": 2}, "unexpected start index"),
    ):
        daily = {
            "columnHeaders": [{"name": "day"}, {"name": "views"}],
            "rows": [["2026-01-15", 2], ["2026-01-16", 3]],
            **extra,
        }
        provider = _provider(daily, aggregate)
        with pytest.raises(AnalyticsResponseError, match=message):
            provider.observe(_query("views"))


def test_pr37_reordered_google_columns_fail_closed():
    daily = {
        "columnHeaders": [
            {"name": "day"},
            {"name": "views"},
            {"name": "likes"},
        ],
        "rows": [["2026-01-16", 5, 2]],
    }
    aggregate = {
        "columnHeaders": [{"name": "likes"}, {"name": "views"}],
        "rows": [[2, 5]],
    }
    provider = _provider(daily, aggregate)

    with pytest.raises(AnalyticsResponseError, match="columns do not match"):
        provider.observe(_query("views", "likes"))


def test_pr37_duplicate_google_columns_fail_closed():
    daily = {
        "columnHeaders": [{"name": "day"}, {"name": "views"}, {"name": "views"}],
        "rows": [["2026-01-16", 5, 5]],
    }
    aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[5]]}
    provider = _provider(daily, aggregate)

    with pytest.raises(AnalyticsResponseError, match="duplicate columns"):
        provider.observe(_query("views"))


def test_pr37_daily_dates_must_be_unique_sorted_and_inside_exact_window():
    aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[5]]}
    for rows, message in (
        ([['2026-01-16', 3], ['2026-01-15', 2]], "not canonical"),
        ([['2026-01-16', 3], ['2026-01-16', 2]], "not canonical"),
        ([['2026-01-15', 2], ['2026-01-17', 3]], "escaped requested window"),
    ):
        daily = {
            "columnHeaders": [{"name": "day"}, {"name": "views"}],
            "rows": rows,
        }
        provider = _provider(daily, aggregate)
        with pytest.raises(AnalyticsResponseError, match=message):
            provider.observe(_query("views"))


def test_pr37_aggregate_must_be_exactly_one_row():
    daily, _aggregate = _valid_views_payloads()
    aggregate = {
        "columnHeaders": [{"name": "views"}],
        "rows": [[5], [6]],
    }
    provider = _provider(daily, aggregate)

    with pytest.raises(AnalyticsResponseError, match="multiple rows"):
        provider.observe(_query("views"))


def test_pr37_count_metric_must_be_nonnegative_integral_and_finite():
    daily, _aggregate = _valid_views_payloads()
    for bad in (1.5, -1, float("nan"), float("inf"), True, "7"):
        aggregate = {"columnHeaders": [{"name": "views"}], "rows": [[bad]]}
        provider = _provider(daily, aggregate)
        with pytest.raises(AnalyticsResponseError):
            provider.observe(_query("views"))


def test_pr37_invalid_or_injected_video_id_never_enters_filter():
    daily, aggregate = _valid_views_payloads()
    provider = _provider(daily, aggregate)
    query = _query("views").model_copy(
        update={
            "publication": _query("views").publication.model_copy(
                update={"remote_id": "video;country==US"}
            )
        }
    )

    with pytest.raises(AnalyticsResponseError, match="invalid YouTube video ID"):
        provider.observe(query)


def test_pr37_window_budget_is_bounded_before_remote_report_query():
    daily, aggregate = _valid_views_payloads()
    provider = _provider(daily, aggregate, max_window_days=1)

    with pytest.raises(AnalyticsExecutionError, match="exceeds local bound"):
        provider.observe(_query("views"))


def test_pr37_wrong_authenticated_channel_is_unavailable_not_retargeted():
    daily, aggregate = _valid_views_payloads()
    provider = _provider(daily, aggregate, channel_id="UC_other_channel")
    health = provider.health()

    assert health.available is False
    assert health.provider_id == "youtube-analytics"


def test_pr37_private_token_check_rejects_symlink_and_group_world_permissions(tmp_path: Path):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    if os.name != "nt":
        token.chmod(0o600)
    assert _safe_token_file(token) is True

    if os.name != "nt":
        token.chmod(0o644)
        assert _safe_token_file(token) is False
        token.chmod(0o600)

    alias = tmp_path / "token-link.json"
    try:
        alias.symlink_to(token)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert _safe_token_file(alias) is False


def test_pr37_auth_target_resolves_parent_alias_before_client_secret_comparison(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    client = real / "client.json"
    client.write_text("{}", encoding="utf-8")
    alias_parent = tmp_path / "alias"
    try:
        alias_parent.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    resolved = _token_target(alias_parent / "token.json")
    assert resolved == real.resolve() / "token.json"
    assert resolved != client.resolve()
    assert _token_target(alias_parent / "client.json") == client.resolve()

    with pytest.raises(RuntimeError, match="paths must be different"):
        authorize_youtube_analytics(
            client_secrets_path=client,
            token_path=alias_parent / "client.json",
            open_browser=False,
        )
