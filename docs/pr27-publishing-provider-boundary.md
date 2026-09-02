# PR27 — Publishing provider boundary and export-to-publish handoff

## Goal

Introduce a platform-agnostic publishing boundary without making remote publishing part of rendering, Project lifecycle, or export correctness.

PR27 is deliberately **not** a YouTube integration. It defines the reusable authority, durability, crash-safety, transport, and PWA contracts that a later YouTube (or other platform) adapter must satisfy.

## Core authority boundary

Publishing begins only after Content Forge already has an authenticated **final** `RenderArtifactManifest`.

- render/review approval authorizes production of the artifact;
- publish approval separately authorizes one exact remote publishing request;
- `Project.state` is not extended with remote-publishing states;
- publishing success/failure never changes the bytes or identity of the render artifact;
- rendering/export remains fully usable when no publishing provider exists;
- the default API/PWA has no remote-publishing capability unless a provider is explicitly injected.

`PublishingService` reuses `RenderOrchestrator.load_artifact()` before candidate creation, approval persistence, and execution. PR27 therefore has no second artifact verifier: existing render evidence remains the sole artifact authority.

## Publish artifact identity

`PublishArtifactRef` is derived only from a final render artifact and pins:

- Project ID;
- render job ID;
- output profile;
- optional variant ID;
- render-plan SHA-256;
- output SHA-256;
- exact byte count;
- dimensions/duration/audio presence.

Preview artifacts are rejected.

## Persistable request

`PublishRequest` is intentionally machine-independent and credential-free. It contains only:

- exact `PublishArtifactRef`;
- credential-free provider/destination identity;
- portable publication metadata.

Filesystem paths are **not** part of the request. `PublishingProvider.publish(...)` receives the authenticated runtime `media_path` separately.

PR27 v1 intentionally has no generic `provider_options` bag. Future platform-specific options must be typed non-secret contracts so tokens, credentials, SDK objects, local paths, or arbitrary provider state cannot enter request identity accidentally.

## Metadata and semantic identity

Portable v1 metadata includes:

- title;
- description;
- bounded unique tags;
- `private | unlisted | public` visibility;
- optional timezone-aware schedule instant.

Schedule instants are canonicalized to UTC before hashing.

`semantic_publish_request_digest()` hashes the complete machine-independent request. `publish_idempotency_key()` is deterministically derived from that digest and is stable across attempts of the same semantic request.

## Exact human approval

`PublishApproval` binds human approval to exactly one request SHA-256. `ApprovedPublishRequest` fails validation if any approved semantic field changes afterward, including artifact bytes, destination, metadata, visibility, or schedule.

The application/PWA flow is intentionally split:

1. build authenticated exact candidate;
2. review its server-computed request digest;
3. approve and durably persist that exact request;
4. execute the already-approved attempt as a separate action.

Changing browser form values after candidate construction invalidates the candidate UI before approval.

## Provider protocol

`PublishingProvider` exposes only:

- `health()` — provider/version availability identity;
- `publish(approved_request, media_path=..., idempotency_key=...)` — remote side effect over an already-approved request.

Provider credentials remain adapter-local and are absent from PR27 request, approval, attempt, result, API, and PWA models.

The provider health snapshot used by an attempt is pinned before remote execution. Its free-form `reason` is stripped before durable persistence so SDK/provider-controlled text cannot leak tokens or headers into SQLite.

## Durable publishing ledger

The additive `publishing` storage component introduces two concepts:

### Operation

A `PublishOperationRecord` represents one semantic request and stores:

- request SHA-256;
- stable idempotency key;
- exact credential-free request;
- creation timestamp.

### Attempt

A `PublishAttemptRecord` has its own `cf_publish_*` identity and one of:

- `prepared` — exact approval is durable; remote execution has **not** begun;
- `running` — remote execution boundary has been crossed;
- `succeeded` — authenticated remote result is durable;
- `failed` — a preflight failure proved no remote side effect occurred;
- `outcome_unknown` — remote side effect may have occurred, so automatic retry is blocked.

`cf_publish_*` is intentionally distinct from `cf_job_*`; publish attempts are not render/background jobs.

`LocalLibrary.publishing` is lazy, so opening a normal library does not create PR27 tables until publishing is actually used.

## Crash and retry semantics

The safety rule is based on whether the remote side effect may have begun.

- `prepared` survives restart unchanged and is resumable;
- preflight failures become `failed` and may be retried through a new attempt;
- abandoned `running` attempts reconcile to `outcome_unknown`;
- `outcome_unknown` blocks automatic retry to avoid duplicate publication;
- repeating execution of an already `succeeded` attempt returns the stored receipt without a second provider call.

`KeyboardInterrupt`/`SystemExit` during remote execution are deliberately not caught as ordinary provider failures; the durable state stays `running`, then restart reconciliation applies the same unknown-outcome rule.

## Privacy boundary

Provider-controlled strings are not persisted as durable failure evidence.

- provider exceptions are chained locally but durable error messages are static Content Forge text;
- provider health `reason` is redacted before persistence;
- `remote_url` must be an absolute HTTP(S) public URL with no userinfo, query, or fragment;
- `remote_id` must be a canonical opaque identifier with no whitespace/control ambiguity;
- PWA/API inputs contain no token, secret, API-key, credential, or filesystem-path fields.

This prevents common SDK errors or callback URLs from accidentally becoming a secret-bearing local audit record.

## Result evidence

A successful provider returns `PublishResult` with:

- `published | scheduled` disposition;
- canonical remote object ID;
- optional safe public remote URL;
- effective timestamp;
- provider/version identity;
- exact request SHA-256;
- stable idempotency key;
- exact output SHA-256;
- destination identity.

`validate_publish_result()` fail-closes if provider health, approval, request identity, artifact digest, destination, provider version, idempotency evidence, or disposition disagree.

Disposition is symmetric:

- an unscheduled request must return `published`;
- a scheduled request must return `scheduled`.

A result mismatch after provider invocation becomes `outcome_unknown`, not retryable `failed`, because a remote object may already exist.

## Authenticated API

PR27 adds bounded authenticated routes for:

- provider/execution status;
- authenticated candidate creation from a final render job;
- exact-digest approval into a durable `prepared` attempt;
- attempt inspection;
- explicit execution of a prepared attempt.

Security properties:

- non-loopback plaintext transport is rejected;
- bearer authentication occurs before JSON parsing;
- publishing JSON is exact `application/json` and bounded to 64 KiB;
- malformed publish IDs fail before storage lookup;
- provider-less execution returns unavailable without consuming the prepared approval;
- no endpoint accepts an arbitrary media path or credential material.

## PWA

The packaged PWA adds a publishing panel with separate controls for:

- building an exact candidate;
- approving that candidate;
- loading an existing durable attempt;
- executing an approved attempt.

The panel displays durable state, request SHA-256, stable idempotency identity, destination, and authenticated result evidence. `prepared`, `running`, `failed`, `outcome_unknown`, and `succeeded` are visibly distinct.

The installed shell is bumped from cache v14 to v15 and precaches `publishing.js`, so installed clients cannot remain on a stale pre-publishing shell after upgrade.

## Non-goals

PR27 still does **not** add:

- YouTube/TikTok/etc. implementation code;
- OAuth login or credential storage;
- a concrete network publishing adapter;
- provider-specific analytics;
- automatic background scheduling daemon;
- automatic retry of unknown remote outcomes.

Those belong in later platform-specific work behind the PR27 contracts.

## Authority chain

The final PR27 authority chain is:

`final RenderArtifactManifest`
→ existing authenticated artifact loader
→ exact credential-free `PublishRequest`
→ human approval of exact request digest
→ durable `prepared` publish attempt
→ pinned provider health/version
→ `running` remote boundary
→ validated provider result
→ durable `succeeded` receipt

Any uncertainty after the `running` boundary terminates as `outcome_unknown` and blocks automatic duplicate publication.
