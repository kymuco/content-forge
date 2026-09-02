# PR35 — Mobile batch Inbox and attention queue

## Goal

PR35 completes the first Daily Production Completion loop by making the phone answer one question first: **what actually needs attention now?**

The desktop remains the local source of truth and compute worker. The phone remains a projection and control surface. PR35 does not create a new project lifecycle, batch job type, approval model, publishing ledger, or browser-owned semantic state.

## Daily attention projection

Authenticated `GET /api/v1/production/attention` projects existing durable state into six product-facing groups:

- `failed` — recovery or uncertainty must be inspected first;
- `attention` — an explicit human decision or side-effect boundary is waiting;
- `safe_work` — existing deterministic Review operations may continue without accepting human authority;
- `working` — preview/final rendering, QC, or remote publication is already in progress;
- `inbox` — unused prepared visual source material is available for Create video;
- `finished` — the local final or durable publication is complete.

The ordering is intentionally safety-first. A visually complete local Project cannot hide a more important unresolved remote publication state.

## No second workflow authority

The queue is read-only. Its group names are not persisted Project states and never become authority.

PR35 derives cards from:

- canonical Inbox receipts and source Projects;
- PR32 production-preset provenance;
- hardened PR10+ Review Project summaries;
- canonical READY authority from the existing Review queue;
- durable PR27 publishing operations and attempts;
- PR34 exact-final publication projection.

A queue card may disappear or move groups only because one of those canonical authorities changed.

## Safe work

Authenticated `POST /api/v1/production/safe-work` accepts only a bounded `render_limit` (`1..12`, default `4`). It does not accept project IDs, task IDs, accepted values, publish attempts, or destinations from the phone.

The coordinator may perform only existing operations whose authority is already complete:

1. bootstrap PR32-derived production Projects that have not entered Review yet;
2. render a preview when the **only** remaining blocking human task is the existing `preview_approval` task and its state is `not_rendered`;
3. render a final only for Projects already present in the hardened Review `ready_projects` projection, which means an exact preview was explicitly approved.

Final render is preferred before preview when the bounded render budget is shared.

Per-project Review failures are quarantined so one failed deterministic operation does not suppress unrelated safe work. The phone receives only the bounded exception type as `error_code`; exception details are not exposed as a new information channel.

## Human authority that PR35 never automates

`safe-work` never:

- accepts or edits hook text;
- accepts crops or source order;
- completes manual source setup;
- accepts optional metadata on behalf of the user;
- approves or rejects a preview;
- changes publication metadata or declarations;
- approves a publish request;
- executes a prepared remote publication;
- retries an `outcome_unknown` publication.

Those operations remain on the existing Project/Review/Publishing surfaces.

## Raw source Projects remain source material

PR32 deliberately creates provenance-preserving derived production Projects instead of mutating raw Inbox source Projects into compound productions. PR35 preserves that distinction.

A raw visual source is excluded from safe compute even if historical/legacy Review data also happens to list that source Project. `Run safe work` therefore cannot turn reusable source material into an accidental Review/final-render workflow.

For attention ergonomics, a source appears under **New sources** only until at least one PR32 production Project records that exact `source_project_id` in canonical preset evidence. This does not consume or delete the source: it remains available in the Create video source catalog for reuse.

## Remote publication risk is not bounded by recent Project history

Ordinary daily Project/source views are intentionally bounded. Remote side-effect risk is not.

PR35 independently reads all durable PR27 attempts in states:

- `prepared`;
- `running`;
- `outcome_unknown`.

Their approved `PublishRequest` artifacts pull the referenced Project back into the daily safety surface even when that Project is outside the ordinary recent-project window and absent from Review Queue.

When multiple active/uncertain publications reference one Project, the strongest state wins for daily classification:

```text
outcome_unknown > running > prepared
```

This remote state also outranks the current local Project lifecycle. A later local recovery state must never make an older uncertain remote side effect look safe or finished.

If an active publishing ledger entry references a Project that can no longer be loaded, PR35 surfaces a recovery card rather than silently dropping the remote state.

The daily queue never executes or retries these attempts. It only makes the unresolved authority visible.

## Phone composition

PR35 does not fork the large PR33 Project controller.

The served `/app/production-home.js` is composed fail-closed from the already-proven PR34 bundle. The composition exports only bounded navigation/refresh hooks through `window.CFProductionHome` and appends the separate `attention-queue.js` module.

`attention-queue.js`:

- reads `/production/attention`;
- calls `/publishing/status` first so existing PR27 reconciliation can classify interrupted remote execution before ranking the queue;
- opens the existing PR33 Project surface;
- opens the existing PR32 Create video wizard;
- invokes only `/production/safe-work` for automatic compute;
- contains no direct review-resolution, preview-approval, publish-approval, or publish-execute call.

The installed shell advances from PR34 `v20` to PR35 `v21`. `v20` remains an explicit predecessor and is removed during the normal stale-cache upgrade path.

## Failure model

The projection fails closed on malformed active publishing evidence instead of pretending the remote state is absent.

Safe compute is bounded and re-validates canonical Review state at execution time. Existing Review render claims, immutable render attempts, preview revision identity, final approval identity, restart recovery, and QC guards remain authoritative; PR35 does not bypass them.

## Exit condition

PR35 is complete when several pieces of source/project work can coexist and the phone presents the user with only the meaningful next layer:

```text
Needs recovery
Needs you
Ready automatically
Working
New sources
Finished
```

while deterministic work can be advanced in a bounded batch without accepting human decisions, and unresolved remote side effects remain visible regardless of ordinary recent-history limits.
