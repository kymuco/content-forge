# PR36 — Analytics provider boundary and durable observation history

## Goal

PR36 begins Milestone 8 by giving Content Forge a trustworthy place to retain performance observations without allowing analytics to become production authority.

Analytics is optional and read-only with respect to production. A machine with no analytics provider continues to ingest, review, render, QC, export, and publish exactly as before. The analytics SQLite component is initialized lazily only when measurement features are used.

## Exact subject: one successful publication

An analytics observation is never attached to a Project ID or remote platform ID alone.

The subject is a `SuccessfulPublicationRef` derived from one durable PR27 `succeeded` publish attempt. It pins:

```text
publish_attempt_id
request_sha256
project_id
render_job_id
output_sha256
publication_provider_id
destination_id
remote_id
disposition
effective_at
```

Before producing this reference, the analytics repository re-validates the stored `PublishResult` against the exact approved `PublishRequest`, approval evidence, provider health, destination, output digest, and disposition using the existing PR27/PR29 validation contract. A syntactically valid but tampered `succeeded` row therefore cannot silently become analytics ground truth.

## Analytics provider boundary

PR36 introduces a platform-independent protocol:

```python
class AnalyticsProvider(Protocol):
    def health(self) -> AnalyticsProviderHealth: ...
    def observe(self, query: AnalyticsQuery) -> AnalyticsObservationBatch: ...
```

The analytics provider is independent from `PublishingProvider`. A publication may have been created by one provider while observations are collected through a different authenticated analytics integration.

Provider health and observation objects are re-materialized through canonical Pydantic validation at the application boundary. This prevents internal `model_copy(update=...)` or equivalent construction shortcuts from bypassing declared invariants.

## Query identity and observation windows

`AnalyticsQuery` contains exactly:

```text
SuccessfulPublicationRef
AnalyticsWindow[start_at, end_at)
requested metric IDs
```

Metric IDs are unique and canonicalized. The semantic query SHA-256 is computed from canonical machine-independent JSON.

Time has three distinct meanings:

- `publication.effective_at` — when the durable publication is considered effective;
- `window.start_at / window.end_at` — the half-open reporting interval requested from the provider;
- `observed_at` — when the provider says this snapshot was observed;
- `ingested_at` — when Content Forge locally persisted the response.

`ingested_at` is deliberately not part of semantic observation identity.

A query window cannot begin before `publication.effective_at`. An observation cannot predate its requested window. `observed_at` may be earlier than `window.end_at`; this preserves a clean path for later PR38 provisional-versus-mature snapshot semantics without changing the PR36 evidence model.

## Typed metrics and missing data

A normalized metric contains:

```text
metric_id
unit
value
```

PR36 supports explicit units:

```text
count
ratio
seconds
currency_minor
score
```

Numeric validation is fail-closed: values must be finite, counts/currency-minor values are non-negative integers, ratios are in `[0, 1]`, and seconds are non-negative.

A zero value is real data. Missing data is represented separately.

Each observation declares one of:

- `complete` — every requested metric was returned;
- `partial` — at least one requested metric was returned and at least one is explicitly missing;
- `unavailable` — no requested metrics were returned and an explicit bounded reason is retained.

Returned and missing metric IDs must exactly partition the requested set. The system never invents zero for an unavailable metric.

## Provider evidence

Every `AnalyticsObservationBatch` retains:

```text
analytics provider ID
analytics provider version
exact query SHA-256
exact publication remote ID
optional provider observation/snapshot ID
```

The returned query must be byte-semantically equivalent to the query issued by Content Forge. Provider ID/version are pinned from the independently validated health response. Query digest and remote publication identity are validated again before persistence.

## Append-only history

PR36 adds a lazy additive SQLite component `analytics` schema v1 with an `analytics_observations` table.

The primary identity is the semantic SHA-256 of the complete observation batch. The table also retains indexed copies of publication/query/provider/window fields for efficient later history queries.

Storage rules:

- exact observation replay is idempotent and returns the original record with its original `ingested_at`;
- a later observation, even for the same publication/window, creates a new record rather than overwriting history;
- there is no update/delete observation API in the PR36 repository;
- stored observation JSON is revalidated on read;
- denormalized index columns are cross-checked against the immutable observation JSON on read;
- current successful-publication evidence is re-authenticated when history is retrieved, because a durable publishing ledger is intended to be immutable rather than silently drift.

This is history-oriented evidence, not a mutable counter cache.

## Analytics service

`AnalyticsService.collect()` performs the sequence:

```text
publish attempt ID
-> re-authenticate exact durable successful publication
-> build canonical AnalyticsQuery
-> provider health
-> canonical health validation
-> provider observe
-> canonical observation + exact evidence validation
-> append-only analytics persistence
```

Any failure before persistence leaves production state and analytics history unchanged.

`AnalyticsService.history()` exposes retained observations for one exact successful publication.

## Authority boundary

Analytics observations cannot:

- mutate a Project;
- resolve a Review task;
- approve a preview;
- start a render;
- alter QC/export evidence;
- create or approve a publish request;
- execute or retry a remote publication;
- silently change future production choices.

Later dashboards, experiments, and recommendations must consume these observations as evidence. Recommendation output remains proposal-only under the roadmap's existing human-authority rules.

## Provider-free behavior

`LocalLibrary` does not initialize analytics schema during normal construction. The component is opened lazily through `library.analytics`, which first ensures the existing publishing ledger is available because analytics observations reference successful publish attempts.

No new required dependency or analytics credential is added by PR36.

## PR37 boundary

PR36 deliberately does **not** implement the YouTube Analytics/Data API adapter, choose a permanent YouTube metric list, schedule polling, or build a dashboard.

PR37 may implement a YouTube analytics provider against this contract. Provider-specific metric availability, API lag, permissions, and partial results must map into PR36's explicit complete/partial/unavailable evidence rather than weakening the generic model.

## Exit condition

PR36 is complete when Content Forge can represent, validate, and append an observation about one exact durable successful publication while preserving:

```text
publication identity
+ reporting window
+ provider/version evidence
+ explicit metric coverage
+ provider observation time
+ separate local ingestion time
```

without introducing any production mutation authority or mandatory provider dependency.
