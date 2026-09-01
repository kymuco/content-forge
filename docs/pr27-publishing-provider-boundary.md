# PR27 — Publishing provider boundary and export-to-publish handoff

## Goal

Introduce a platform-agnostic publishing boundary without making remote publishing part of rendering, Project lifecycle, or export correctness.

PR27 is deliberately **not** a YouTube integration. It defines the contracts a later YouTube (or other platform) adapter must satisfy.

## Core authority boundary

Publishing begins only after Content Forge already has a verified **final** `RenderArtifactManifest`.

- render/review approval authorizes production of the artifact;
- publish approval separately authorizes one exact remote publishing request;
- `Project.state` is not extended with remote-publishing states;
- publishing success/failure never changes the bytes or identity of the render artifact;
- rendering/export remains fully usable when no publishing provider exists.

## Publish artifact identity

`PublishArtifactRef` is derived from a final render artifact and pins:

- Project ID;
- render job ID;
- output profile;
- optional variant ID;
- render-plan SHA-256;
- output SHA-256;
- exact byte count;
- dimensions/duration/audio presence.

The helper rejects preview artifacts. A later durable publishing service must obtain the source manifest through the existing authenticated render-artifact loader rather than trusting an arbitrary caller-created model.

## Persistable request

`PublishRequest` is intentionally machine-independent and credential-free. It contains only:

- exact `PublishArtifactRef`;
- credential-free provider/destination identity;
- portable publication metadata.

Filesystem paths are **not** part of the request. The `PublishingProvider.publish(...)` runtime call receives `media_path` separately after orchestration has authenticated the artifact bytes.

PR27 v1 also intentionally has no generic `provider_options` bag. Platform-specific options must be added later as typed non-secret adapter contracts so access tokens/credentials do not accidentally enter durable request/approval records.

## Metadata

Portable v1 metadata includes:

- title;
- description;
- bounded unique tags;
- `private | unlisted | public` visibility;
- optional timezone-aware schedule instant.

Schedule instants are canonicalized to UTC before semantic hashing so two timezone representations of the same instant have the same request identity.

## Exact human approval

`semantic_publish_request_digest()` hashes the complete machine-independent request.

`PublishApproval` binds human approval to that SHA-256. `ApprovedPublishRequest` fails validation if any approved semantic field changed afterward, including artifact bytes, target, title, description, tags, visibility, or schedule.

This approval is intentionally distinct from preview/final-render approval.

## Provider protocol

`PublishingProvider` exposes only:

- `health()` — stable provider/version availability identity;
- `publish(approved_request, media_path=...)` — remote side effect over an already approved exact request.

Provider credentials remain implementation-local and are absent from all PR27 public result/evidence models.

## Result evidence

A successful provider returns `PublishResult` with:

- published/scheduled disposition;
- remote object ID;
- optional safe absolute HTTP(S) URL without URL userinfo;
- effective timestamp;
- provider/version identity;
- exact request SHA-256;
- exact output SHA-256;
- destination identity.

`validate_publish_result()` fail-closes if provider health, approved request, artifact digest, destination, or returned evidence disagree. A provider cannot claim a scheduled result for a request that had no schedule.

## First-slice non-goals

This slice intentionally does **not** add:

- YouTube/TikTok/etc. credentials;
- OAuth;
- actual network publishing;
- SQLite publish attempts/receipts;
- API/PWA publish buttons;
- automatic scheduling;
- analytics.

Those layers come only after the provider/approval identity is green and reviewed.

## Next PR27 layers

After this contract passes CI:

1. durable publish attempt/receipt repository;
2. idempotency and crash/retry semantics;
3. authenticated orchestration that re-loads and verifies final artifacts before provider invocation;
4. bounded API/PWA review/approval surface;
5. only later, a concrete platform adapter in a separate PR.
