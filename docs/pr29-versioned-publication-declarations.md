# PR29 — Versioned publication declarations contract v2

## Goal

Make publication-policy declarations explicit human-approved publish semantics without changing the identity of historical PR27/PR28 publishing operations.

PR29 adds a second publish-contract generation rather than silently extending the PR27 digest payload. This is necessary because fields such as YouTube's made-for-kids declaration and realistic altered/synthetic-media disclosure can change the remote publication state and therefore must be visible before approval.

## Contract generations

Content Forge supports two publish contract versions after PR29:

- `pr27_publish_contract_v1` — the historical PR27/PR28 shape;
- `pr29_publish_contract_v2` — the same artifact/target/metadata authority plus explicit publication declarations.

A v1 request contains no declarations. A v2 request requires both declarations.

```text
PublishDeclarations
├── child_directed: bool
└── contains_realistic_altered_or_synthetic_media: bool
```

The booleans are intentionally required. There is no provider-local default and the PWA does not silently select `false`. The declaration model uses strict boolean validation, so JSON strings or integers such as `"false"`, `0`, and `1` cannot be coerced into human-approved declaration values.

## Why this is a versioned contract

PR27 defined semantic request identity as a SHA-256 over:

```text
contract_version
artifact
publish target
portable metadata
```

Simply adding declaration fields to that historical payload would make old persisted approvals fail validation after an upgrade even when their original publication intent had not changed.

PR29 therefore freezes the v1 digest algorithm byte-for-byte. For v1, the hash payload remains exactly the historical four-field shape. The new root `declarations` object is included only when the request explicitly declares `pr29_publish_contract_v2`.

As a result:

- historical v1 request SHA-256 values remain stable;
- historical `cfp-<sha256>` idempotency keys remain stable;
- existing SQLite operation and attempt rows remain readable;
- old prepared v1 attempts can still execute after upgrading Content Forge;
- changing either v2 declaration necessarily creates a different semantic digest and idempotency key.

`PublishApproval.contract_version` and `PublishInvocationEvidence.contract_version` must match the request generation. A provider result from one contract generation cannot authenticate a request from another.

## Human authority

The v2 authority chain is:

```text
final RenderArtifactManifest
→ authenticated artifact identity
→ title / description / tags / visibility / schedule
→ explicit child-directed declaration
→ explicit realistic altered/synthetic declaration
→ exact v2 semantic digest
→ human approval
→ durable prepared attempt
→ provider preflight
→ remote execution
→ remote declaration verification
→ authenticated result
```

The declarations are part of the request shown before approval. Editing either declaration invalidates the PWA candidate exactly like editing title, destination, visibility, or schedule.

## YouTube mapping

For `pr29_publish_contract_v2`, the YouTube adapter maps the approved declarations to the Data API `status` object:

```text
child_directed
→ status.selfDeclaredMadeForKids

contains_realistic_altered_or_synthetic_media
→ status.containsSyntheticMedia
```

The second declaration is deliberately named narrowly. It means realistic content that was meaningfully altered or synthetically generated for the platform disclosure field; it is not a generic declaration that any use of AI occurred somewhere in the production pipeline.

The PR29 provider uses the proven PR28 OAuth, channel binding, preflight, authenticated snapshot, resumable upload, processing polling, scheduling, and failure semantics. PR29 changes only the contract-aware publication metadata layer.

For v2 uploads:

1. PR28 preflight authenticates the exact media snapshot and constructs a local upload request.
2. PR29 replaces that still-local request with one containing the exact approved declarations.
3. The durable `running` boundary is recorded.
4. Remote upload begins only at `next_chunk()`.
5. PR28 waits for `status.uploadStatus == processed` and verifies ordinary metadata/privacy/schedule.
6. PR29 performs an owner-authorized status read and requires both remote declaration values to equal the approved v2 request.
7. Result evidence explicitly records `pr29_publish_contract_v2`.

A mismatch or missing declaration after remote upload is not converted into a successful receipt. It raises after the remote boundary and therefore falls into the existing retry-blocking `outcome_unknown` safety rule.

## Legacy v1 YouTube behavior

PR29 does not retrofit declaration values into old v1 attempts.

When a v1 request is executed:

- `selfDeclaredMadeForKids` is not injected by Content Forge;
- `containsSyntheticMedia` is not injected by Content Forge;
- the historical request digest is retained;
- the PR28 media/upload semantics remain unchanged.

This is intentional. Inventing new declaration values while executing an already-approved historical request would create remote semantics that the human never approved.

## API compatibility

`POST /api/v1/publishing/candidates` remains backward compatible.

A legacy client that omits both new properties creates a v1 candidate:

```json
{
  "render_job_id": "cf_job_...",
  "target": {"provider_id": "youtube", "destination_id": "UC..."},
  "metadata": {"title": "...", "visibility": "private"}
}
```

A v2 client supplies both:

```json
{
  "contract_version": "pr29_publish_contract_v2",
  "declarations": {
    "child_directed": false,
    "contains_realistic_altered_or_synthetic_media": true
  }
}
```

Transport validation rejects a v2 candidate with missing declarations and rejects declaration fields on a v1 candidate.

`GET /api/v1/publishing/status` advertises `pr29_publish_contract_v2` as the preferred contract generation for current clients.

## PWA behavior

The packaged publishing PWA creates v2 candidates by default.

Both declarations are rendered as required Yes/No controls with an initially empty choice. The candidate cannot be built until the operator explicitly answers both questions. The exact candidate card and durable-attempt card display the selected declarations and contract version.

Changing either field invalidates the current candidate before approval. The existing exact-attempt binding still prevents executing a different manually edited attempt ID.

The service-worker shell cache advances to v16 so installed clients cannot remain pinned to the older publishing form after the contract upgrade.

## Storage compatibility

PR29 requires no SQLite schema migration.

Existing `publish_operations.request_json` values may lack both `contract_version` on `PublishRequest` and the `declarations` property because those fields did not exist when PR27 records were written. Current model decoding interprets those missing request fields as v1 / no declarations.

The v1 semantic digest intentionally excludes the newly introduced model properties, so a historical stored `request_sha256` still validates. Re-ensuring the same operation normalizes the old JSON through the current model without creating an identity collision.

Regression coverage inserts an actual pre-v2 operation/attempt JSON shape into the publishing tables and verifies that the current repository loads and re-validates it under the original digest.

## Provider version

The public runtime now reports:

```text
youtube_data_api_v3_pr29_v2:category=22:notify=0:decl=2
```

The existing immutable PR28 policy remains:

- category `22`;
- `notifySubscribers=false`.

Those values are still not mutable outside human approval. Making them selectable requires a future versioned semantic extension rather than an unapproved provider setting.

## Non-goals

PR29 does not add:

- automatic classification of child-directed content;
- automatic inference of altered/synthetic-media disclosure;
- legal or policy advice about whether either declaration should be true;
- YouTube category selection;
- subscriber-notification controls;
- thumbnails, captions, playlists, monetization, or analytics;
- a generic cross-platform policy engine;
- another publishing provider;
- automatic retries of unknown remote outcomes.

The operator supplies the declarations. Content Forge's responsibility is to make the choices explicit, bind them to approval identity, carry them to the provider exactly, and fail closed when the remote state cannot be authenticated.
