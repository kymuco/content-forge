# Render orchestration

## Purpose

PR7 adds the small persistence/orchestration layer between a compiled renderer-independent `RenderPlan` and the FFmpeg backend.

The boundary is intentionally narrow:

```text
Project + template resolver
        |
        v
    RenderPlan
        |
        v
 RenderOrchestrator
  | submit snapshot
  | persistent job state
  | execute FFmpeg
  | verify artifact
  | write sidecar manifest
        |
        v
runtime/renders/<project>/<job>/
```

This layer does **not** decide content kind, template behavior, review policy, publishing, or batch scheduling. It receives an already validated `RenderPlan` and makes one preview/final render attempt persistent and auditable.

## Why this exists before the phone service

PR5 proved generic rendering and PR6 proved the first real presentation template. Before exposing that path through a phone-facing service, the project needs one stable unit of work with durable inputs and outputs. PR7 therefore freezes a minimal render-job contract that later API/PWA and batch workers can call instead of invoking FFmpeg directly.

This is not the later full batch queue: no worker pool, retry scheduler, crash recovery scan, or automatic batching is introduced here.

## Persistent job contract

A submitted render creates a normal PR3 `StoredJob` with:

- `job_type = "render"`;
- initial state `queued`;
- immutable payload containing render purpose, selected profile/variant/template identity, render-plan digest, and canonical runtime storage keys;
- a complete `RenderPlan` JSON snapshot stored beside the future output.

The plan snapshot is the authoritative execution input for that job. A later project edit cannot silently change an already-submitted render.

Execution uses compare-and-set job state transitions so two workers cannot both claim the same queued job.

Supported v1 states are:

```text
queued -> running -> succeeded
                 -> failed
                 -> cancelled
```

## Runtime layout

Artifacts are runtime data and remain outside the source repository:

```text
renders/
  <project_id>/
    <job_id>/
      plan.json
      artifact.<container>
      artifact-manifest.json
      failure-manifest.json   # only for failed/cancelled attempts
```

Storage keys persisted in SQLite and manifests are relative to `CONTENT_FORGE_HOME`; absolute machine paths are not used as artifact identity.

## Preview/final purpose

PR7 accepts only explicit `preview` or `final` purpose. The selected `OutputProfile.properties["purpose"]` must match the submitted purpose.

This prevents a caller from labeling a final profile as an approved preview, while keeping preview/final as output-profile policy rather than template identity.

The orchestrator does not force an encoder by purpose. NVENC preference remains a backend execution option with the existing CPU fallback.

## Successful artifact manifest

A successful render writes a validated sidecar containing:

- manifest version;
- job/project/purpose/profile/variant/template identity;
- render-plan digest;
- FFmpeg command-manifest digest;
- source asset IDs and SHA-256 digests from the plan;
- output storage key and SHA-256 digest;
- encoder and FFmpeg version;
- byte size and elapsed time;
- ffprobe-confirmed dimensions, duration, frame rate, codecs, and audio presence;
- completion timestamp.

Before the job becomes `succeeded`, PR7 verifies that the published file is non-empty, hashes it, probes it, and checks the probed width/height against the selected output profile. If post-render verification or manifest publication fails, the output is removed and the job does not become successful.

## Failure manifest

Failed and cancelled attempts write a bounded structured failure sidecar when possible. Native `FFmpegBackendError` fields are preserved (`code`, `stage`, return code, diagnostic details) without changing the backend exception contract.

A failure sidecar is diagnostic state, not a successful artifact. Successful execution removes a stale failure sidecar before publishing success.

## Integrity and restart behavior

`run_job(job_id)` reloads the persisted `plan.json` instead of trusting an in-memory plan. It verifies that:

- the job belongs to the requested project;
- the persisted plan digest matches the immutable job payload;
- profile/variant/template identity still matches the payload;
- runtime storage keys exactly match the canonical job directory;
- source assets resolve through the existing content-addressed runtime storage policy by default.

This makes the unit of work restartable without introducing a scheduler yet.

## Deliberate limits

PR7 does not add:

- FastAPI or phone upload endpoints;
- PWA/review UI;
- background workers;
- retry/backoff policy;
- batch submission;
- automatic project/template compilation from a job payload;
- publishing;
- full QC such as loudness, black-frame detection, or text-visual inspection.

Those layers can build on the persistent render-job contract without moving workflow semantics into FFmpeg.