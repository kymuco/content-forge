# PR10 — Review queue and proxy preview

PR10 completes the phone-first production-control milestone above the merged PR8 durable Inbox and PR9 PWA. It does not introduce a second renderer, a browser-side editor, a worker pool, or a new project schema. The implementation operationalizes the review contracts that already exist in `Project.review_tasks` and routes rendering through the frozen PR7 render-attempt boundary.

## Architectural boundary

```text
canonical Project in SQLite
    ↓
ReviewService manifest CAS mutation
    ↓
compile_hook_overlay / normalized RenderPlan
    ↓
RenderOrchestrator.submit(purpose="preview"|"final")
    ↓
immutable PR7 RenderPlan snapshot
    ↓
RenderOrchestrator.run_job()
    ↓
authenticated artifact manifest + verified MP4
```

The phone never supplies FFmpeg arguments, paths, render plans, arbitrary project patches, or output storage keys. It can only resolve a small whitelist of review decisions, request a preview/final render, and approve or reject an authenticated preview artifact.

## Existing contracts reused

PR10 deliberately reuses `AttentionMode`, `ReviewTask`, `ReviewPriority`, `ReviewStatus`, `ProjectState`, the existing `shorts_preview` 540×960 and `shorts_final` 1080×1920 profiles, `hook_overlay`, PR7 persistent render jobs, PR8 single-owner storage, and PR9 bearer/PWA infrastructure. No schema migration is needed because review tasks are already canonical project state.

## Review bootstrap

For a prepared image/video Inbox project, explicit bootstrap selects or creates a review variant, derives scene order from existing authoritative source references when needed, attaches the built-in `hook_overlay` template when no template is selected, adds preview/final profiles, creates the bounded review task set, and moves the project to `needs_review`.

Bootstrap is idempotent and never changes asset bytes or source provenance. If the current project cannot be represented by this built-in visual workflow, PR10 creates a blocking `MANUAL` `source_setup` task instead of guessing a transformation.

The PWA bulk-prepare action is server-side and enumerates the complete safe Project set rather than a capped intake-history page. Non-renderable projects retain a deterministic fingerprint of only setup-relevant inputs; unchanged projects are skipped without issuing another MANUAL re-entry receipt, while a real source/template/scene/profile/asset change permits one durable recheck. A malformed project or asset encountered while selecting one candidate is quarantined as a bounded per-project failure and does not prevent independent eligible projects from being prepared.

## Attention semantics and queue order

`AUTO` records deterministic work. `timeline_bootstrap` is retained as a resolved AUTO task but hidden from the ordinary human queue.

`REVIEW` is bounded phone work: `hook`, `crop_confirmation`, optional `source_order`, non-blocking `metadata`, and `preview_approval`.

`MANUAL` marks work that cannot safely be represented by the current phone surface.

The queue ranks blocking before non-blocking work, then `BLOCKING → HIGH → NORMAL → LOW`, with MANUAL before REVIEW at an equal rank.

## Canonical mutation and concurrency

Review writes are compare-and-set operations over the exact stored project manifest:

```text
read manifest_json A
→ validate decision against Project A
→ produce validated Project B
→ UPDATE ... WHERE project_id = ? AND manifest_json = A
```

A concurrent decision cannot blindly overwrite a newer project. PR10 edits do not alter source-reference membership, so the established `project_assets` relation remains valid while scene order/crop, variant text, task state, template/profile identity, metadata, and lifecycle state change.

## Phone-editable decisions

The HTTP API is not a generic project PATCH surface.

- `hook` accepts one bounded non-empty string.
- `crop_confirmation` requires an exact entry for every current scene; a value is `null` for full frame or a validated normalized rectangle.
- `source_order` must be an exact permutation of current scene IDs.
- `metadata` accepts only title, description, and bounded hashtags.

Any accepted edit invalidates the current preview candidate and removes prior approval metadata.

## 540×960 proxy preview

All blocking human tasks except `preview_approval` must be resolved before preview. PR10 compiles the canonical project with the existing `shorts_preview` output profile and freezes it through PR7 `RenderOrchestrator`.

The still-open preview task records the render job ID, exact render-plan digest, output SHA-256, and verified dimensions. A succeeded artifact may be reused only when purpose and render-plan digest match, and the PR7 sidecar must still validate.

PR10 rendering is synchronous. Background workers/batch scheduling remain PR17 work.

## Approval, rejection, and stale-preview protection

Approval recompiles the current preview plan and requires its digest to equal the candidate digest and the authenticated PR7 artifact digest. Any project change makes the preview stale and returns a conflict.

Approval resolves `preview_approval`, stores the approved preview job/digest, and moves a project with no remaining blocking human work to `ready`.

Rejection leaves the project in `needs_review`, records the rejected preview, and reopens resolved editable REVIEW decisions so the phone can perform the bounded reject → edit → rerender loop.

## Final render and lifecycle

```text
inbox
→ needs_review
→ ready
→ rendering
→ qc
→ done
```

Final render is permitted only while the currently compiled preview digest still equals the explicitly approved digest. Final output uses the same project/template/variant semantics with `shorts_final` 1080×1920 and the PR7 render-attempt boundary.

A final receipt is authoritative only when its complete stored `{job, render-plan digest, output SHA-256}` identity matches both the authenticated PR7 artifact and the **currently compiled canonical final plan**. Restart recovery applies the same semantic check before adopting a persisted `RENDERING` claim into QC. Therefore a repair/import/generic save cannot retain an older final receipt or active digest and silently replay it against changed render inputs.

Semantic staleness and artifact loss are intentionally different recovery classes. If the canonical plan is unchanged but the completed artifact is missing/corrupt, the already-approved project may return to `ready` for a fresh immutable final attempt. If the canonical plan itself changed, PR10 clears stale final and preview receipts, reopens the bounded edit plus `preview_approval` lifecycle from current canonical values, and returns the project to `needs_review`; it is never stranded as an unactionable READY/QC/DONE project.

PR7 output hash/dimension/ffprobe verification is the QC baseline available in PR10, so a verified artifact advances `rendering → qc → done`. Runtime render failure returns the project to `ready` with a bounded diagnostic rather than publishing synthetic success.

## HTTP security boundary

All review/project/render-job API routes require a live PR8 bearer. PR10 middleware authenticates before downstream body parsing. JSON mutation bodies require `application/json`, valid `Content-Length`, and a 128 KiB pre-parse cap. Matching is mount/root-path aware.

Artifact download accepts only a job ID, validates the PR7 artifact sidecar, and derives the runtime path internally. The phone neither supplies nor receives raw filesystem paths.

## PWA surface

The PR9 shell gains an online Review panel. Packaged `review.js` uses the mount-scoped persisted bearer, can prepare Inbox projects, resolves bounded review cards, requests the proxy, displays authenticated MP4 bytes via temporary Blob URLs, approves/rejects preview, and triggers final render for READY projects.

Untrusted values are placed with DOM `textContent`/element construction rather than `innerHTML`. Review/render operations are online-only and do not mutate the PR9 IndexedDB ingest queue.

## Out of scope

PR10 does not add arbitrary project PATCH, browser-side render plans/FFmpeg, a worker pool, PR11 template registry/plugin loading, new template families, automatic publishing, downloader/scraper behavior, OCR/TTS/LLM providers, or PR17 batch QC.

## Exit condition

A prepared visual Inbox project can be taken through bounded phone decisions, a verified 540×960 preview, explicit preview approval, and a final verified render without requiring desktop interaction for the ordinary path.
