# PR37 — YouTube Analytics adapter

PR37 implements the first concrete adapter behind the PR36 read-only `AnalyticsProvider` boundary. It imports performance observations only for exact Content Forge publications already proven successful by the durable publishing ledger.

It does **not** add a dashboard, experiment engine, recommendation engine, historical channel crawler, or any authority to mutate production state.

## Boundary

The runtime remains:

```text
durable PR27 succeeded publication
-> PR36 SuccessfulPublicationRef
-> exact PR36 AnalyticsQuery
-> YouTubeAnalyticsProvider
-> YouTube Analytics API v2
-> validated complete / partial / unavailable observation
-> PR36 append-only analytics history
```

The adapter never receives a loose Project ID or arbitrary user-entered video URL. Its subject already pins:

- publish attempt ID and request digest;
- exact Project/render/output identity;
- publication provider and destination channel;
- exact remote YouTube video ID;
- publish disposition and effective time.

PR36 revalidates the underlying successful publish evidence before constructing this subject, and PR37 independently requires `publication_provider_id == "youtube"` plus the configured destination channel.

## Separate read-only OAuth capability

PR37 deliberately does not widen the proven PR28/PR29 publishing token.

Publishing retains its existing scopes:

```text
https://www.googleapis.com/auth/youtube.upload
https://www.googleapis.com/auth/youtube.readonly
```

Analytics uses a separate local authorized-user token with only:

```text
https://www.googleapis.com/auth/yt-analytics.readonly
https://www.googleapis.com/auth/youtube.readonly
```

`youtube.readonly` is used to resolve the authenticated channel exactly. `yt-analytics.readonly` permits read-only YouTube Analytics reports. No monetary analytics scope is requested in PR37.

Authorize the analytics capability separately:

```text
content-forge-youtube-analytics-auth \
  --client-secrets /absolute/path/client_secret.json \
  --token /absolute/private/path/youtube-analytics-token.json
```

The CLI requires an offline refresh token, verifies that authorization resolves exactly one YouTube channel, and prints that channel ID for local configuration. The token is provider-local runtime state and never enters PR36 semantic query/evidence identity.

The token target follows the same safety model as publishing credentials: final-component symlinks are rejected, parent aliases are resolved before comparing the token path with the client-secrets path, and POSIX token reads require a private owner-controlled regular file.

## Provider identity and local config

The provider ID is:

```text
youtube-analytics
```

`YouTubeAnalyticsConfig` contains only local runtime settings:

```text
token_path
channel_id
max_retries
max_window_days
```

Those values are configuration, not analytics semantic identity. Provider version and the exact PR36 query digest are retained in every observation.

`health()` loads the separate analytics token, uses YouTube Data API v3 only to resolve `channels.list(mine=True)`, requires exactly one channel, and requires that ID to match `config.channel_id`. A successful health check retains one authenticated YouTube Analytics API v2 service only for the immediately following same-thread observation call.

No remote mutation endpoint is used.

## Exact video binding

For an accepted PR36 subject, the report query uses:

```text
ids=channel==MINE
filters=video==<exact SuccessfulPublicationRef.remote_id>
```

The remote video ID is validated as a canonical YouTube-style opaque identifier before interpolation into the filter. A malformed/injected filter value fails closed before any analytics query.

The publication destination channel must also equal the configured/authenticated channel. PR37 never retargets an observation to another channel or video.

## Reporting-window semantics

PR36 uses timezone-aware half-open windows:

```text
[start_at, end_at)
```

YouTube Analytics reports use calendar dates in Pacific Time and an inclusive `endDate`. Google also defines reporting days using Pacific local time, including daylight-saving transitions.

PR37 therefore accepts only PR36 windows whose start and end are **exact Pacific local midnights**. It converts:

```text
PR36 start_at                  -> Google startDate
PR36 exclusive end_at - 1 day -> Google inclusive endDate
```

This is DST-aware through `America/Los_Angeles`; it is not implemented as a fixed UTC offset. For example, two Pacific reporting days across the March DST transition can span 47 physical hours and are still exactly two reporting days.

Sub-day or merely UTC-midnight windows are rejected instead of pretending YouTube supplies precision it does not expose through this report shape.

`max_window_days` places a local upper bound on one provider request before any report call.

## Late-data and completeness proof

YouTube documents that reports can stop at the last date for which all requested metrics are available. Accepting an aggregate blindly could therefore attach a shorter hidden interval to the larger PR36 window.

PR37 uses a two-query protocol with the **same supported metric set**:

1. daily coverage query: `dimensions=day`, `sort=day`, exact video filter, and `maxResults` equal to the number of possible reporting days;
2. aggregate query over the same exact dates/video/metrics, but only after the daily evidence proves the requested final reporting day is present.

The daily response must have exact requested column order, unique sorted dates, dates inside the requested interval, and the requested inclusive end day as its final row. Explicit embedded API error evidence, an unexpected result kind/start index, duplicate/reordered columns, malformed row widths, or out-of-window dates fail closed.

If no daily rows exist, the observation is `unavailable`. If recent YouTube reporting has not reached the requested final day, the observation is also `unavailable` and the aggregate query is not executed.

This does not claim that every calendar day must have a row; it proves only that YouTube reports availability through the exact requested end day before an aggregate is accepted.

## Initial metric map

PR37 intentionally starts with additive, non-monetary metrics supported together by YouTube channel time-based reports with a video filter:

| Content Forge metric | YouTube metric | Unit |
| --- | --- | --- |
| `views` | `views` | `count` |
| `engaged_views` | `engagedViews` | `count` |
| `watch_time_seconds` | `estimatedMinutesWatched` | `seconds` after exact ×60 conversion |
| `likes` | `likes` | `count` |
| `comments` | `comments` | `count` |
| `shares` | `shares` | `count` |
| `subscribers_gained` | `subscribersGained` | `count` |
| `subscribers_lost` | `subscribersLost` | `count` |

Count values must be finite, non-negative integers. Watch minutes must be finite/non-negative before conversion.

PR37 does not include revenue/RPM, which would require a wider monetary analytics capability. It also does not synthesize derived average-duration metrics in this adapter; later comparable-window work can define derived summaries over retained additive evidence with explicit semantics.

## Missing, partial, and unavailable data

PR36's distinction remains authoritative:

- `complete`: every requested metric was returned;
- `partial`: at least one requested metric was returned and at least one is explicitly missing/unsupported;
- `unavailable`: no trustworthy requested metric observation can be attached to this window.

Examples of explicit unavailable outcomes include:

```text
youtube_reporting_window_not_closed
youtube_metric_set_unsupported
youtube_reporting_window_no_data
youtube_reporting_window_incomplete
youtube_aggregate_no_data
youtube_requested_metrics_unavailable
```

No report rows are **not** converted into zero views. Unsupported metrics are **not** converted into zero. Late data is **not** treated as a complete shorter window.

Returned numeric zero is retained as a real observed zero only when YouTube actually returns that value for the accepted report.

## Response integrity

PR37 fails closed when provider responses violate the requested shape, including:

- embedded Google `errors` evidence;
- unexpected report kind or non-default start index;
- duplicate/reordered/missing columns;
- malformed row widths;
- duplicate, unsorted, invalid, or out-of-window day values;
- multiple aggregate rows;
- non-numeric, non-finite, negative, Boolean, string, or fractional count values;
- publication/channel/video mismatch.

PR36 then independently revalidates the resulting observation, exact query digest, provider identity/version, publication identity, coverage partition, and append-only storage evidence.

## Optional runtime

PR37 reuses the existing `youtube` optional dependency group:

```text
pip install 'content-forge[youtube]'
```

The base Content Forge install still does not require Google libraries, OAuth credentials, a YouTube account, or analytics storage initialization. The existing YouTube CI optional-contract job tests both the proven publishing adapter and PR37 analytics adapter with fake/injected services, including the PR37 adversarial hardening suite; normal tests never require a live Google account.

## Non-goals

PR37 does not implement:

- monetary/revenue metrics;
- arbitrary historical channel/video import;
- sub-day YouTube reporting windows;
- retention-curve ingestion;
- dashboard/PWA analytics presentation;
- comparable/mature window summaries;
- experiments or causal attribution;
- automated recommendations;
- any production or remote mutation authority.

Those remain later Milestone 8 steps built over retained PR36 observations.

## Official API references used for this contract

- YouTube Analytics API `reports.query`: <https://developers.google.com/youtube/analytics/reference/reports/query>
- YouTube Analytics channel reports: <https://developers.google.com/youtube/analytics/channel_reports>
- YouTube Analytics metrics: <https://developers.google.com/youtube/analytics/metrics>
- YouTube Analytics authorization: <https://developers.google.com/youtube/analytics/guides/authentication>
