# PR34 — Final-to-publish phone handoff

PR34 completes the ordinary Project-specific phone path from an authenticated `DONE` final into the existing PR27–PR29 publishing authority.

It does **not** introduce a second publishing model, publish ledger, remote side-effect path, destination store, or approval contract. The phone surface is a convenience projection over exact final-render evidence plus the existing publishing candidate / approval / execution boundaries.

## Product flow

```text
Project screen
-> authenticated final is DONE
-> Publish stage appears
-> use configured credential-free destination
-> edit title / description / tags / visibility / optional schedule
-> explicitly answer PR29 publication declarations
-> build exact publish candidate
-> review exact request + final SHA-256
-> approve exact request
-> separately execute durable prepared attempt
-> observe durable remote result / failure / unknown outcome
```

The normal path never asks the user to paste a Project ID, render job ID, provider ID, destination/channel ID, publish attempt ID, token path, or credential.

## Existing authority reused

PR34 continues to use the established mutations:

```text
POST /api/v1/publishing/candidates
POST /api/v1/publishing/attempts
POST /api/v1/publishing/attempts/{attempt_id}/execute
GET  /api/v1/publishing/attempts/{attempt_id}
GET  /api/v1/publishing/status
```

Candidate identity, semantic SHA-256, idempotency key, exact `PublishArtifactRef`, durable approval, attempt lifecycle, provider health pinning, preflight semantics, `outcome_unknown`, and successful receipt validation remain owned by PR27–PR29.

Render approval and publish approval remain separate human acts. Building or approving a publish request does not begin remote execution.

## Exact-final project projection

PR34 adds one authenticated read-only convenience route:

```text
GET /api/v1/publishing/projects/{project_id}
    ?render_job_id={exact_final_job_id}
    &output_sha256={exact_final_sha256}
    &limit={bounded_limit}
```

This route does not create or mutate publishing rows. It reads the existing PR27 operation/attempt ledger and returns only attempts whose stored `PublishRequest.artifact` matches all three exact fields:

- `project_id`;
- `render_job_id`;
- `output_sha256`.

The limit is applied **after** exact-final filtering. Newer attempts for another Project or for an older/different final of the same Project therefore cannot hide the current final's durable publication state.

The response repeats the exact `project_id`, `render_job_id`, and `output_sha256`. The served phone bundle validates all three against the current canonical Project summary before using the projection.

## Final identity handoff

PR33 already projects the current successful final as:

- final render `job_id`;
- final render-plan digest;
- final output SHA-256;
- authenticated artifact endpoint.

PR34 passes `project.final.job_id` into the existing candidate endpoint. The publishing service then independently reloads and authenticates the final render artifact and constructs the canonical `PublishArtifactRef`.

The client verifies the candidate returned by the server still contains the exact current:

- Project ID;
- final render job ID;
- final output SHA-256;
- configured publish target;
- PR29 v2 contract version.

Client checks are defense in depth only. The existing publishing service remains authoritative.

## Credential-free configured destination

A publishing provider may optionally expose a safe `configured_target()` projection returning only a canonical `PublishTarget`.

For the production YouTube adapter this is:

```text
provider_id: youtube
destination_id: configured channel ID
```

The OAuth token path, refresh token, credentials, Google client state, filesystem paths, and other provider-local configuration are never part of this projection.

Unknown providers that do not explicitly expose a safe target are not guessed. The routine phone surface falls back to Advanced publishing or local-only final use.

Provider-free Content Forge remains fully usable for ingest, review, render, QC, playback, and export.

## Human publication decisions

The routine phone form retains only decisions that materially belong to the publish request:

- title;
- description;
- tags;
- visibility;
- optional schedule;
- explicit `child_directed` Yes/No;
- explicit `contains_realistic_altered_or_synthetic_media` Yes/No.

PR34 does not infer either PR29 declaration from content, Project metadata, OCR, TTS usage, templates, or provider state.

Project metadata is used only as editable initial text for title/description/tags. It does not bypass publish candidate construction or exact approval.

## Durable attempt recovery

Refreshing or reopening the Project screen does not create a new client-side publication state. PR34 reads the durable PR27 ledger after calling the existing publishing status route so interrupted `running` attempts receive the established reconciliation semantics first.

For one exact final, routine UI state is chosen conservatively. Remote-risk states outrank successful/local states:

```text
outcome_unknown
> running
> succeeded
> prepared
> failed
```

This prevents a historical successful receipt from hiding a second attempt whose remote outcome is still unknown or whose execution may still be running.

Multiple `prepared` or multiple `running` attempts for one exact final are treated as inconsistent routine state and block one-tap execution. Advanced inspection is required instead of guessing which attempt is safe.

## Attempt-state behavior

### `prepared`

The exact human approval is durable and remote execution has not started. If a provider is configured, the phone offers a separate explicit execution action. Without a provider, the approved attempt remains local and durable.

### `running`

Remote execution may be in progress. PR34 offers refresh only and does not create replacement requests.

### `succeeded`

The exact final already has a validated durable publication receipt. The routine surface shows the stored remote identity/URL when present and does not create another upload automatically.

### `failed`

The existing publishing contract uses `failed` only for a failure before a trusted remote side effect. PR34 may therefore offer creation of another exact request. The new request still receives a new semantic digest according to its actual metadata/declarations.

### `outcome_unknown`

Remote execution began but no authenticated outcome was recorded. Automatic replacement/retry is blocked to avoid duplicate publication. PR34 preserves that rule and routes the user to Advanced inspection rather than inventing a recovery action.

## Phone/PWA composition

PR34 keeps the existing Project controller and Advanced publishing module intact.

The server deterministically composes two non-authoritative UI events into the served Project controller:

- `content-forge:project-flow-rendered` with the current Project summary;
- `content-forge:project-flow-closed`.

The Project publishing module attaches its stage only for canonical `DONE` Projects with final job/hash evidence. The composition markers are checked fail-closed so a future controller refactor cannot silently attach the feature at the wrong lifecycle location.

The served publishing bundle also composes exact-final query/response guards and conservative attempt-state priority over the packaged project publishing module. Marker drift fails closed rather than silently weakening the routine flow.

The installed PWA shell advances from v19 to v20. The PR33 v19 cache remains an explicit predecessor.

## Advanced publishing remains available

The existing Advanced publishing surface is intentionally retained. It remains useful for:

- unsupported providers without a safe target projection;
- manual inspection of attempt IDs and exact digests;
- recovery/investigation when routine state is ambiguous;
- provider configuration/debugging.

PR34 makes it unnecessary for the ordinary happy path; it does not delete engineering controls.

## Security properties retained

PR34 preserves the established boundaries:

- paired bearer authentication for publishing reads and mutations;
- non-loopback publishing transport remains HTTPS-only;
- no filesystem paths in semantic publish identity or phone payloads;
- no credentials in configured target projection;
- exact authenticated final artifact before candidate construction and again before execution;
- exact publish request digest confirmation before durable approval;
- PR29 declarations remain explicit human authority;
- approval does not execute;
- preflight failures remain retry-safe `failed` states;
- post-running failures become retry-blocking `outcome_unknown`;
- successful replay remains idempotent by existing attempt identity;
- no browser-side durable semantic authority;
- no automatic publication from final-render completion.

## Non-goals

PR34 does not add:

- analytics or performance observations;
- automatic title/tag/declaration inference;
- thumbnails, captions, playlists, or second publishing platforms;
- automatic retry of unknown remote outcomes;
- batch publishing;
- a new destination/profile database;
- changes to YouTube category or subscriber-notification policy;
- changes to render/QC semantics;
- changes to PR27–PR29 semantic request identity.

PR35 owns the mobile batch Inbox / attention experience. Measurement and recommendation work remains after Daily Production Completion.
