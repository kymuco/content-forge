"""PR37 YouTube Analytics API v2 adapter over the PR36 evidence boundary."""

from __future__ import annotations

import math
import os
import stat
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import local
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from content_forge.core.models import FrozenModel

from .analytics import (
    AnalyticsExecutionError,
    AnalyticsInvocationEvidence,
    AnalyticsMetric,
    AnalyticsObservationBatch,
    AnalyticsProviderHealth,
    AnalyticsQuery,
    AnalyticsResponseError,
    AnalyticsUnavailableError,
    semantic_analytics_query_digest,
)
from .youtube_auth import write_private_token

_PROVIDER_ID = "youtube-analytics"
_PROVIDER_VERSION = "youtube_analytics_api_v2_pr37_v1:pt-day:nonmonetary"
_YOUTUBE_PUBLICATION_PROVIDER_ID = "youtube"
_YOUTUBE_ANALYTICS_READ_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
_YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_ANALYTICS_OAUTH_SCOPES = (
    _YOUTUBE_ANALYTICS_READ_SCOPE,
    _YOUTUBE_READ_SCOPE,
)
_PACIFIC = ZoneInfo("America/Los_Angeles")

# PR37 intentionally starts with additive, non-monetary core metrics. Average-duration
# and monetary metrics need different aggregation/scope semantics and are not guessed.
_METRIC_MAP: dict[str, tuple[str, str]] = {
    "views": ("views", "count"),
    "engaged_views": ("engagedViews", "count"),
    "watch_time_seconds": ("estimatedMinutesWatched", "seconds"),
    "likes": ("likes", "count"),
    "comments": ("comments", "count"),
    "shares": ("shares", "count"),
    "subscribers_gained": ("subscribersGained", "count"),
    "subscribers_lost": ("subscribersLost", "count"),
}

CredentialsLoader = Callable[[Path], object]
ServiceFactory = Callable[[object], Any]
Clock = Callable[[], datetime]


class YouTubeAnalyticsConfig(FrozenModel):
    """Local-only analytics credentials/channel binding; never semantic evidence."""

    token_path: str = Field(min_length=1, max_length=4096)
    channel_id: str = Field(min_length=3, max_length=128)
    max_retries: int = Field(default=5, ge=0, le=10)
    max_window_days: int = Field(default=366, ge=1, le=3660)

    @field_validator("token_path")
    @classmethod
    def validate_token_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("token_path must contain non-whitespace content")
        path = Path(normalized).expanduser()
        if not path.is_absolute():
            raise ValueError("YouTube Analytics token_path must be absolute local runtime state")
        if path.name in {"", ".", ".."}:
            raise ValueError("YouTube Analytics token_path must identify a file")
        return str(path)

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            character.isspace() or ord(character) < 32 for character in normalized
        ):
            raise ValueError("YouTube Analytics channel_id must be canonical")
        return normalized


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_token_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if os.name == "nt":
        return True
    try:
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
    except OSError:
        return False
    if mode & 0o077:
        return False
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        return False
    return True


def _load_credentials(token_path: Path) -> object:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except Exception as exc:  # pragma: no cover - optional dependency environment
        raise AnalyticsUnavailableError(
            "YouTube Analytics dependencies are not installed"
        ) from exc

    if not _safe_token_file(token_path):
        raise AnalyticsUnavailableError(
            "YouTube Analytics OAuth token must be a regular private local file"
        )
    try:
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            scopes=list(YOUTUBE_ANALYTICS_OAUTH_SCOPES),
        )
    except Exception as exc:
        raise AnalyticsUnavailableError("YouTube Analytics OAuth token could not be loaded") from exc

    has_scopes = getattr(credentials, "has_scopes", None)
    if callable(has_scopes) and not has_scopes(YOUTUBE_ANALYTICS_OAUTH_SCOPES):
        raise AnalyticsUnavailableError("YouTube Analytics OAuth token lacks required scopes")

    if not getattr(credentials, "valid", False):
        if not getattr(credentials, "expired", False) or not getattr(
            credentials, "refresh_token", None
        ):
            raise AnalyticsUnavailableError("YouTube Analytics OAuth token is not refreshable")
        try:
            credentials.refresh(Request())
            write_private_token(token_path, credentials.to_json())
        except Exception as exc:
            raise AnalyticsUnavailableError(
                "YouTube Analytics OAuth token refresh failed"
            ) from exc
    return credentials


def _build_analytics_service(credentials: object) -> Any:
    try:
        from googleapiclient.discovery import build
    except Exception as exc:  # pragma: no cover - optional dependency environment
        raise AnalyticsUnavailableError(
            "google-api-python-client is not installed for YouTube Analytics"
        ) from exc
    try:
        return build(
            "youtubeAnalytics",
            "v2",
            credentials=credentials,
            cache_discovery=False,
        )
    except Exception as exc:
        raise AnalyticsUnavailableError("YouTube Analytics API client initialization failed") from exc


def _build_data_service(credentials: object) -> Any:
    try:
        from googleapiclient.discovery import build
    except Exception as exc:  # pragma: no cover - optional dependency environment
        raise AnalyticsUnavailableError(
            "google-api-python-client is not installed for YouTube channel verification"
        ) from exc
    try:
        return build("youtube", "v3", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise AnalyticsUnavailableError("YouTube Data API client initialization failed") from exc


def _execute_object(request: Any, *, retries: int, label: str) -> dict[str, object]:
    try:
        payload = request.execute(num_retries=retries)
    except Exception as exc:
        raise AnalyticsExecutionError(f"YouTube {label} request failed") from exc
    if not isinstance(payload, dict):
        raise AnalyticsResponseError(f"YouTube {label} returned a non-object response")
    embedded_errors = payload.get("errors")
    if embedded_errors not in (None, []):
        raise AnalyticsResponseError(f"YouTube {label} returned embedded error evidence")
    return payload


def _canonical_video_id(value: str) -> str:
    if not value or len(value) > 128 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in value
    ):
        raise AnalyticsResponseError("analytics subject contains an invalid YouTube video ID")
    return value


def _reporting_dates(query: AnalyticsQuery, *, max_window_days: int) -> tuple[date, date, int]:
    start = query.window.start_at.astimezone(_PACIFIC)
    end = query.window.end_at.astimezone(_PACIFIC)

    def exact_midnight(value: datetime) -> bool:
        local_midnight = datetime.combine(value.date(), time.min, tzinfo=_PACIFIC)
        return value == local_midnight

    if not exact_midnight(start) or not exact_midnight(end):
        raise AnalyticsExecutionError(
            "YouTube Analytics requires windows aligned to Pacific reporting-day boundaries"
        )
    day_count = (end.date() - start.date()).days
    if day_count < 1:
        raise AnalyticsExecutionError("YouTube Analytics reporting window is empty")
    if day_count > max_window_days:
        raise AnalyticsExecutionError("YouTube Analytics reporting window exceeds local bound")
    # PR36 end is exclusive while the Google API endDate is inclusive.
    return start.date(), end.date() - timedelta(days=1), day_count


def _headers(payload: dict[str, object], *, label: str) -> tuple[str, ...]:
    kind = payload.get("kind")
    if kind is not None and kind != "youtubeAnalytics#resultTable":
        raise AnalyticsResponseError(f"YouTube {label} returned an unexpected result kind")
    start_index = payload.get("startIndex")
    if start_index is not None and start_index != 1:
        raise AnalyticsResponseError(f"YouTube {label} returned an unexpected start index")
    raw = payload.get("columnHeaders")
    if not isinstance(raw, list) or not raw:
        raise AnalyticsResponseError(f"YouTube {label} lacks column headers")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AnalyticsResponseError(f"YouTube {label} contains an invalid column header")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise AnalyticsResponseError(f"YouTube {label} contains an unnamed column")
        names.append(name)
    if len(names) != len(set(names)):
        raise AnalyticsResponseError(f"YouTube {label} contains duplicate columns")
    return tuple(names)


def _rows(payload: dict[str, object], *, width: int, label: str) -> tuple[tuple[object, ...], ...]:
    raw = payload.get("rows")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AnalyticsResponseError(f"YouTube {label} rows are invalid")
    parsed: list[tuple[object, ...]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != width:
            raise AnalyticsResponseError(f"YouTube {label} row width is invalid")
        parsed.append(tuple(row))
    return tuple(parsed)


def _parse_day(value: object) -> date:
    if not isinstance(value, str):
        raise AnalyticsResponseError("YouTube daily coverage contains a non-date value")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AnalyticsResponseError("YouTube daily coverage contains an invalid date") from exc


def _numeric(value: object, *, metric: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalyticsResponseError(f"YouTube metric {metric} is not numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise AnalyticsResponseError(f"YouTube metric {metric} is invalid")
    return value


def _normalized_metric(metric_id: str, api_name: str, raw: object) -> AnalyticsMetric | None:
    value = _numeric(raw, metric=api_name)
    if value is None:
        return None
    _mapped_name, unit = _METRIC_MAP[metric_id]
    if unit == "count":
        numeric = float(value)
        if not numeric.is_integer():
            raise AnalyticsResponseError(f"YouTube count metric {api_name} is not integral")
        normalized: int | float = int(numeric)
    elif metric_id == "watch_time_seconds":
        normalized = float(value) * 60.0
    else:  # pragma: no cover - mapping intentionally tiny and explicit
        normalized = float(value)
    return AnalyticsMetric(metric_id=metric_id, unit=unit, value=normalized)


def _unavailable(
    query: AnalyticsQuery,
    *,
    observed_at: datetime,
    reason: str,
) -> AnalyticsObservationBatch:
    return AnalyticsObservationBatch(
        query=query,
        observed_at=observed_at,
        availability="unavailable",
        metrics=(),
        missing_metric_ids=query.metric_ids,
        unavailable_reason=reason,
        evidence=AnalyticsInvocationEvidence(
            provider_id=_PROVIDER_ID,
            provider_version=_PROVIDER_VERSION,
            query_sha256=semantic_analytics_query_digest(query),
            publication_remote_id=query.publication.remote_id,
        ),
    )


class YouTubeAnalyticsProvider:
    """Read-only YouTube Analytics API v2 adapter for exact Content Forge publications."""

    def __init__(
        self,
        config: YouTubeAnalyticsConfig,
        *,
        credentials_loader: CredentialsLoader | None = None,
        analytics_service_factory: ServiceFactory | None = None,
        data_service_factory: ServiceFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self._credentials_loader = _load_credentials if credentials_loader is None else credentials_loader
        self._analytics_service_factory = (
            _build_analytics_service if analytics_service_factory is None else analytics_service_factory
        )
        self._data_service_factory = _build_data_service if data_service_factory is None else data_service_factory
        self._clock = _utc_now if clock is None else clock
        self._thread_state = local()

    def _clear(self) -> None:
        if hasattr(self._thread_state, "analytics_service"):
            del self._thread_state.analytics_service

    def health(self) -> AnalyticsProviderHealth:
        """Verify local token plus exact authenticated channel without changing remote state."""

        self._clear()
        try:
            credentials = self._credentials_loader(Path(self.config.token_path))
            data_service = self._data_service_factory(credentials)
            payload = _execute_object(
                data_service.channels().list(part="id", mine=True, maxResults=2),
                retries=self.config.max_retries,
                label="channel verification",
            )
            items = payload.get("items")
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise AnalyticsUnavailableError(
                    "YouTube Analytics authorization did not resolve exactly one channel"
                )
            if items[0].get("id") != self.config.channel_id:
                raise AnalyticsUnavailableError(
                    "YouTube Analytics authenticated channel does not match configuration"
                )
            self._thread_state.analytics_service = self._analytics_service_factory(credentials)
            return AnalyticsProviderHealth(
                provider_id=_PROVIDER_ID,
                provider_version=_PROVIDER_VERSION,
                available=True,
                reason=None,
            )
        except Exception:
            self._clear()
            return AnalyticsProviderHealth(
                provider_id=_PROVIDER_ID,
                provider_version=_PROVIDER_VERSION,
                available=False,
                reason="YouTube Analytics runtime is unavailable",
            )

    def observe(self, query: AnalyticsQuery) -> AnalyticsObservationBatch:
        service = getattr(self._thread_state, "analytics_service", None)
        self._clear()
        if service is None:
            raise AnalyticsUnavailableError(
                "YouTube Analytics observation requires a successful provider health check"
            )
        publication = query.publication
        if publication.publication_provider_id != _YOUTUBE_PUBLICATION_PROVIDER_ID:
            raise AnalyticsExecutionError("analytics subject is not a YouTube publication")
        if publication.destination_id != self.config.channel_id:
            raise AnalyticsResponseError(
                "analytics subject channel does not match configured YouTube channel"
            )
        video_id = _canonical_video_id(publication.remote_id)
        start_date, end_date, day_count = _reporting_dates(
            query,
            max_window_days=self.config.max_window_days,
        )

        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise AnalyticsExecutionError("YouTube Analytics provider clock must be timezone-aware")
        observed_at = observed_at.astimezone(timezone.utc)
        if observed_at < query.window.start_at:
            raise AnalyticsExecutionError("YouTube Analytics cannot observe a future reporting window")
        if observed_at < query.window.end_at:
            return _unavailable(
                query,
                observed_at=observed_at,
                reason="youtube_reporting_window_not_closed",
            )

        requested_supported = tuple(metric for metric in query.metric_ids if metric in _METRIC_MAP)
        unsupported = tuple(metric for metric in query.metric_ids if metric not in _METRIC_MAP)
        if not requested_supported:
            return _unavailable(
                query,
                observed_at=observed_at,
                reason="youtube_metric_set_unsupported",
            )

        api_metrics = tuple(_METRIC_MAP[metric][0] for metric in requested_supported)
        metrics_arg = ",".join(api_metrics)
        common = {
            "ids": "channel==MINE",
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "metrics": metrics_arg,
            "filters": f"video=={video_id}",
        }

        # Google documents that reports stop at the last date for which all requested
        # metrics are available. A daily probe makes that truncation visible before we
        # accept an aggregate as evidence for the full PR36 window. maxResults is exact
        # to the number of possible reporting days so pagination cannot hide the end day.
        daily = _execute_object(
            service.reports().query(
                **common,
                dimensions="day",
                sort="day",
                maxResults=day_count,
            ),
            retries=self.config.max_retries,
            label="daily coverage",
        )
        daily_headers = _headers(daily, label="daily coverage")
        if daily_headers != ("day",) + api_metrics:
            raise AnalyticsResponseError("YouTube daily coverage columns do not match request")
        daily_rows = _rows(daily, width=len(daily_headers), label="daily coverage")
        if not daily_rows:
            return _unavailable(
                query,
                observed_at=observed_at,
                reason="youtube_reporting_window_no_data",
            )
        days = tuple(_parse_day(row[0]) for row in daily_rows)
        if len(days) != len(set(days)) or tuple(sorted(days)) != days:
            raise AnalyticsResponseError("YouTube daily coverage dates are not canonical")
        if days[0] < start_date or days[-1] > end_date:
            raise AnalyticsResponseError("YouTube daily coverage escaped requested window")
        if days[-1] != end_date:
            return _unavailable(
                query,
                observed_at=observed_at,
                reason="youtube_reporting_window_incomplete",
            )

        aggregate = _execute_object(
            service.reports().query(**common),
            retries=self.config.max_retries,
            label="aggregate analytics",
        )
        aggregate_headers = _headers(aggregate, label="aggregate analytics")
        if aggregate_headers != api_metrics:
            raise AnalyticsResponseError("YouTube aggregate columns do not match request")
        aggregate_rows = _rows(
            aggregate,
            width=len(aggregate_headers),
            label="aggregate analytics",
        )
        if not aggregate_rows:
            return _unavailable(
                query,
                observed_at=observed_at,
                reason="youtube_aggregate_no_data",
            )
        if len(aggregate_rows) != 1:
            raise AnalyticsResponseError("YouTube aggregate analytics returned multiple rows")

        returned: list[AnalyticsMetric] = []
        missing: list[str] = list(unsupported)
        for metric_id, api_name, raw in zip(
            requested_supported,
            api_metrics,
            aggregate_rows[0],
            strict=True,
        ):
            metric = _normalized_metric(metric_id, api_name, raw)
            if metric is None:
                missing.append(metric_id)
            else:
                returned.append(metric)

        if not returned:
            return _unavailable(
                query,
                observed_at=observed_at,
                reason="youtube_requested_metrics_unavailable",
            )
        availability = "complete" if not missing else "partial"
        return AnalyticsObservationBatch(
            query=query,
            observed_at=observed_at,
            availability=availability,
            metrics=tuple(returned),
            missing_metric_ids=tuple(missing),
            unavailable_reason=None,
            evidence=AnalyticsInvocationEvidence(
                provider_id=_PROVIDER_ID,
                provider_version=_PROVIDER_VERSION,
                query_sha256=semantic_analytics_query_digest(query),
                publication_remote_id=publication.remote_id,
            ),
        )


__all__ = [
    "YOUTUBE_ANALYTICS_OAUTH_SCOPES",
    "YouTubeAnalyticsConfig",
    "YouTubeAnalyticsProvider",
]
