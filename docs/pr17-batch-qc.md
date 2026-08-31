# PR17 — Batch preparation, render queue, QC, and reproducibility

PR17 closes the intended v0.1 batch-production boundary without adding a second renderer or a parallel job system.

The existing PR7 render attempt remains the atomic executable unit:

```text
Batch job
  ├─ item_0000 -> PR7 render attempt 0 -> optional recovery attempt 1...
  ├─ item_0001 -> PR7 render attempt 0 -> optional recovery attempt 1...
  └─ item_000N -> PR7 render attempt 0 -> optional recovery attempt 1...
```

Every child attempt still owns its immutable `plan.json`, FFmpeg command manifest, artifact/failure manifest, source-byte verification, and authenticated terminal SQLite receipts.

## Preparation snapshot

`BatchCoordinator.prepare(...)` freezes one `BatchItemSnapshot` per intended output. The batch item records:

- project, profile and preview/final purpose;
- exact variant ID/language where present;
- exact template ID/version where present;
- render-plan digest;
- sorted source asset IDs and SHA-256 hashes;
- exact PR16 `LocalizedVariantSnapshot` for variant renders;
- rendered text from the already-compiled `RenderPlan`;
- resolved review accepted values;
- provider metadata only for provider suggestions whose value exactly matches the accepted review value.

Rendered text is intentionally the text in the frozen render plan, including deterministic wrapping performed by a template. It is not re-read from the current `Project` after batch preparation.

For a project with variants, the caller must supply the exact PR16 localized snapshot used during compilation. Preparation fails closed if stored localized project metadata changed between compilation and batch preparation.

## Held initial render attempts

Initial child render attempts are created with state:

```text
batch_held
```

They already contain their immutable `batch_context` in the first SQLite row:

```text
batch_job_id
item_key
attempt_index
```

This is deliberate. A batch child must never appear as an ordinary runnable `queued` job before the parent batch manifest is committed.

The child plan file is written before the child SQLite row. Therefore a process crash before the INSERT may leave an unreferenced directory, but it cannot leave a runnable job without batch identity. Once the parent batch is committed as `queued` and execution begins, the coordinator releases each child `batch_held -> queued` immediately before normal PR7 claiming.

No SQLite schema migration is required: parent and child records remain ordinary `StoredJob` rows.

## Execution and crash recovery

A committed parent batch follows:

```text
queued -> running -> succeeded | failed
```

A child follows the existing PR7 terminal states after release:

```text
batch_held -> queued -> running -> succeeded | failed | cancelled
```

The coordinator writes a fresh `batch_run_instance_id` before handing a queued child to PR7 execution. Two interruption windows are handled explicitly:

1. **Claim written, PR7 render not started.** A child is still `queued` but carries an older run-instance ID. The old attempt is terminalized with authenticated `batch_claim_interrupted` evidence, then a new attempt is created.
2. **PR7 render was running.** A child is `running` and belongs to an older process run. It is terminalized with authenticated `render_interrupted` evidence, then a new attempt is created.

The replacement attempt is created from the previous attempt's persisted `plan.json`. It does **not** recompile or revalidate against mutable current Project presentation metadata. The render-plan digest must equal the frozen batch-item digest, and PR7 still verifies source bytes against planned SHA-256 immediately before FFmpeg execution.

This makes recovery mean “another attempt at the same accepted render intent,” not “render whatever the project means now.”

A process interruption during the uncommitted `preparing` phase is different: the batch has not yet established an authenticated complete manifest and therefore must fail closed rather than reconstruct accepted metadata from mutable project state. Held children are non-runnable until a committed batch run releases them.

## Live ownership and attempt lookup

Restart recovery is allowed only when no other live process owns the same batch drain. The public `BatchCoordinator.run_batch(...)` acquires a non-blocking OS advisory lock at:

```text
batches/<batch_job_id>/.run.lock
```

The lock is held across the entire drain. A concurrent live runner therefore fails before it can reinterpret the first runner's child state as stale recovery evidence. The lock file may survive a crash, but OS lock ownership does not: when the owning process/file descriptor disappears, a new process can acquire the lock immediately without a TTL or wall-clock staleness guess.

The public drain also builds render-attempt history once per batch. One deterministic scan of persistent render jobs is grouped by batch ID, item key, and attempt index, after which per-item recovery uses that in-memory index. This keeps normal drain lookup `O(all render jobs + batch items)` rather than rescanning all historical render jobs once per item. Newly created interruption-recovery attempts are appended to the active index as they are created.

## QC contract

`run_render_qc(...)` combines hard structural evidence with analyses that are only authoritative when the required evidence/runtime exists.

Each check has one of three states:

- `pass` — evaluated and satisfied;
- `fail` — evaluated and violated;
- `not_evaluable` — the current artifact/plan does not contain enough evidence or the optional analysis failed.

`not_evaluable` is explicit and never silently rewritten to `pass`. Checks can be non-blocking where the architecture cannot yet promise a universal evaluator.

Current checks:

### Blocking structural checks

- dimensions exactly match the frozen output profile;
- duration remains within the frame-aware tolerance of the frozen timeline;
- artifact source fingerprints match the frozen plan;
- a timeline that requires audio produced an audio stream;
- deterministic text-layout evidence does not exceed its frozen width/height budget;
- text/border placement does not intersect configured protected safe zones.

For text paths without deterministic layout evidence, overflow is reported as non-blocking `not_evaluable` rather than guessed from character count.

### Black-frame analysis

When FFmpeg analysis is available, PR17 uses `blackdetect` and records measured black duration/ratio. The current blocking boundary rejects an artifact only when it is predominantly black. Analysis failure is reported as non-blocking `not_evaluable`.

### Loudness and true peak

When the frozen output profile contains PR14 audio policy/mastering evidence, PR17 analyzes the final artifact and applies the existing PR14 loudness/true-peak evaluator. Audio without a frozen PR14 mastering policy remains explicit non-blocking `not_evaluable`; missing audio required by the timeline is blocking.

## Render success versus batch/QC success

A PR7 render job and a PR17 batch item have intentionally different authority:

- PR7 `succeeded` means a verified media artifact was rendered and its integrity receipts are valid.
- PR17 item `succeeded` additionally requires blocking QC to pass and a reproducibility/export sidecar to be published.

Therefore a valid rendered file can remain a successful PR7 artifact while the containing batch item fails QC. PR17 does not rewrite historical render-job success merely because a later QC policy rejects that artifact.

## Export sidecar

A successful or QC-evaluated item writes a portable `ExportSidecar` containing:

- batch/item/render job identity;
- project/profile/purpose/variant/template identity;
- frozen render-plan digest;
- source asset hashes;
- accepted rendered text and review values;
- accepted provider suggestion parameters/evidence;
- localized variant digest when applicable;
- Content Forge FFmpeg backend version;
- concrete FFmpeg version and selected video encoder;
- command-manifest digest;
- final output SHA-256 and storage key;
- artifact-manifest storage key;
- complete QC report.

Each `BatchItemResult` records the exact SHA-256 digest of its export sidecar. The complete `BatchResultManifest` contains those item digests and is itself SHA-256 authenticated by append-only fields in the parent SQLite job payload. Because the QC report is embedded in the export sidecar, the terminal chain is:

```text
parent SQLite receipt
  -> batch-result digest
    -> item export-sidecar digest
      -> export sidecar
        -> QC report + render/output/source evidence
```

Changing terminal export/QC metadata therefore changes evidence already committed by the parent batch receipt.

## Runtime layout

PR17 uses runtime-relative paths only:

```text
batches/<batch_job_id>/
  batch-manifest.json
  batch-result.json
  .run.lock
  items/<item_key>/
    qc-report.json
    export-sidecar.json

renders/<project_id>/<render_job_id>/
  plan.json
  command-manifest.json
  artifact.mp4
  artifact-manifest.json
  failure-manifest.json
```

Render attempt evidence remains owned by PR7 under `renders/`; the batch layer references it rather than copying or replacing it.

## Deliberate boundaries

PR17 does not add:

- automatic publishing;
- a second FFmpeg renderer;
- a second database queue;
- automatic semantic retries after ordinary render/QC failures;
- mutation of the Project from batch output;
- provider calls during batch execution;
- a generic distributed worker pool.

Only interruption recovery automatically creates a new render attempt, and that retry is bound to the exact frozen persisted plan.

## Exit condition

PR17 is complete when a committed batch can render preview/final items, survive process interruption without changing accepted intent, reject concurrent live ownership, evaluate explicit QC, and publish authenticated reproducibility/export evidence using the existing PR7 job/runtime contracts.
