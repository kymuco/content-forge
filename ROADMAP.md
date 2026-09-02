# Roadmap

This roadmap is organized as small reviewable pull requests. Content Forge should become useful early and remain replaceable at the edges: providers, templates, components, publishing adapters, analytics integrations, and product surfaces can evolve without destabilizing the canonical Project or renderer core.

## Current implementation status

- PR1–PR34: **complete** in the intended post-merge repository state.
- Milestones 0–6: **complete**.
- Milestone 7A — Publishing: **complete through the first production YouTube path**.
- Milestone 7B — Daily Production Completion: **current product phase**.
- Current implemented step: **PR34 — Final-to-publish phone handoff**.
- Next product step: **PR35 — Mobile batch Inbox and attention queue**.
- Analytics/experiments remain planned **after** the daily phone production loop is genuinely convenient.
- The intended **v0.1 batch-production boundary remains complete through PR17**; later PRs extend the same runtime rather than replacing it.

The engine is already durable through publication:

```text
Source
-> Ingest
-> Project
-> Review
-> Render
-> QC / export
-> Human-approved publish request
-> YouTube
```

The immediate product goal is to hide that engine behind the original phone-first workflow:

```text
Find/share on phone
-> Production Home
-> Create video
-> choose a useful human-facing format
-> choose/order compatible source media
-> answer only the decisions that need judgment
-> preview on phone
-> approve
-> desktop renders/QCs
-> final on phone
-> publish
```

The desktop remains the local source of truth and compute worker. The phone is the normal daily control surface. Internal subsystem panels remain available for advanced/debugging work, but routine production should not require knowledge of project IDs, render job IDs, template versions, provider internals, or repository architecture.

---

## Milestone 0 — Foundation

Status: **complete**.

### PR1 — Architecture, taxonomy, workflow, and v0.1 contract

Established product boundaries, content-kind/template/workflow separation, scene/timeline architecture, provider boundaries, source provenance, repository hygiene, and the staged implementation plan.

---

## Milestone 1 — Core runtime and first useful vertical slice

Status: **complete**.

### PR2 — Python package bootstrap and core domain contracts
Validated canonical domain models, stable IDs/schema versions, serialization, tests, and CI.

### PR3 — Local asset store, hashing, SQLite metadata, and provenance
Content-addressed local storage, SHA-256 identity/deduplication, project/asset/job metadata, source provenance, and repository/runtime separation.

### PR4 — Scene/timeline model and deterministic timeline compiler
Canonical ordered scene graph plus deterministic renderer-independent compilation.

### PR5 — FFmpeg/ffprobe backend and capability probing
Generic FFmpeg compiler/runner, ffprobe verification, capability detection, CPU/NVENC selection, structured errors, and synthetic integration coverage.

### PR6 — First template: `hook_overlay`
First complete 1080x1920 vertical format over the generic renderer.

### PR7 — Preview/final render orchestration and artifact manifests
Durable render attempts, immutable plan snapshots, preview/final identity, verified output artifacts, reproducibility sidecars, and restart-safe execution.

---

## Milestone 2 — Phone-first production infrastructure

Status: **complete at the transport/review layer**.

### PR8 — Authenticated local FastAPI, durable Inbox ingest, and media preparation
Authenticated local ingest, durable staging/recovery, content-addressed acceptance, probing/thumbnails, Inbox project creation, single-owner runtime, and secure LAN boundary.

### PR9 — PWA shell and share-to-Inbox flow
Installable phone-first PWA, Android share target, Inbox UI, upload recovery, and pairing/onboarding.

### PR10 — Review queue and proxy preview
Explicit review authority, fast previews, approve/reject/edit loop, phone controls, and final-production lifecycle.

Milestone 2 proved the asymmetric architecture: phone for capture/judgment, desktop for durable state and compute. Milestone 7B completes the everyday product ergonomics over that authority rather than introducing a second mobile architecture.

---

## Milestone 3 — Template/component system and initial format coverage

Status: **complete**.

### PR11 — Template registry, skins, slots, and component contracts
Versioned declarative template/component/skin registry and extension boundary.

### PR12 — Initial template pack
Registered initial non-voiced formats including hook, social-post, meme, framed-content, art-story, panel-sequence, sync-stack, and reaction compositions.

### PR13 — Reusable overlay and motion components
Reusable credits/comments/reactions/avatars/watermarks, pan/zoom/reveal motion, transitions, and text-overflow checks.

### PR14 — Music and audio composition
Deterministic music/original mixing, ducking/fades, loudness/peak QC, and cached lossless premaster handling.

---

## Milestone 4 — Optional intelligence and variants

Status: **complete**.

### PR15 — LLM provider boundary and `chatgpt-web-adapter`
Optional replaceable language/semantic assistance with proposal-only authority; rendering remains provider-independent.

### PR16 — Language variants and localized render metadata
Shared-source localized variants with deterministic variant-specific render/cache identity.

### PR17 — Batch preparation, render queue, QC, and reproducibility
Crash-safe batch coordination, frozen render plans, batch preview/final rendering, QC, and reproducibility evidence.

### v0.1 release boundary

Status: **implemented through PR17**.

```text
Phone discovery
-> Share/upload over Wi-Fi
-> Inbox
-> automatic ingest/probe
-> select or accept template
-> review small decisions on phone
-> fast preview
-> final render
-> batch/QC
-> MP4 + reproducibility metadata export
```

Publishing, voiced stories, and analytics were outside the original v0.1 boundary and were added later without replacing the v0.1 renderer/runtime.

---

## Milestone 5 — Voiced panels and persistent cast

Status: **complete**.

### PR18 — OCR provider and panel text extraction workflow
Retained local OCR output, geometry/confidence evidence, bounded correction authority, and replaceable OCR provider boundary.

### PR19 — Dialogue scene model and speaker assignment
Human-approved reading order, narrative speaker identity, character registry, provenance revalidation, and semantic focus hints.

### PR20 — TTS provider and Qwen TTS integration
Replaceable per-line TTS boundary, pinned Qwen integration, semantic cache identity, verified WAV evidence, and content-addressed generated audio.

### PR21 — Persistent Voice Cast registry
Persistent reusable cast identity, character-to-cast bindings, immutable cast revisions, project-local overrides, preview synthesis, and exact reference-audio integrity.

### PR22 — Voiced story review UI and timed text
Panel-centric voiced-story review, listen/regenerate controls, verified audio-derived scene timing, phrase-level editorial timed text, reversible materialization, and render guards.

### PR23 — Voiced scene audio mix and camera choreography
Dialogue/presentation QC, music/ambience ducking, reversible camera choreography, focus intent, preview lifecycle invalidation, and real FFmpeg integration.

Milestone exit condition achieved: a panel sequence can become a reviewed, voiced, timed, mixed, camera-presented render without introducing a second timeline or renderer authority.

---

## Milestone 6 — Long-form and reusable production assets

Status: **complete**.

### PR24 — Long-form output profiles
16:9 1080p/1440p output through the existing timeline/render architecture, chapter metadata, authenticated render reuse, and exact cross-project shared voiced-scene references.

### PR25 — Project / series / channel production profiles
Revisioned reusable production defaults for branding, templates/skins, cast, languages, credit policy, output profiles, music/reaction libraries, with explicit reversible Project binding.

### PR26 — Production library search and tagging
Indexed bounded tags, Unicode-safe search/prefix search, virtual collections, duplicate lookup, source-reuse history, used/unused filters, authenticated API, and PWA library surface.

---

## Milestone 7A — Publishing and exact remote authority

Status: **complete through the first YouTube production path**.

Publishing remains optional. Rendering/export is always usable without a publishing provider.

### PR27 — Publishing provider boundary and export-to-publish handoff
Platform-agnostic credential-free `PublishRequest`, exact human approval, semantic/idempotency identity, durable publish ledger, replaceable provider boundary, explicit remote side-effect boundary, and fail-closed `outcome_unknown` semantics.

### PR28 — YouTube Data API publishing adapter
Installed-app OAuth, exact channel binding, authenticated immutable media snapshot, resumable YouTube upload/scheduling, processing verification, exact remote metadata verification, provider-local secret boundary, and optional YouTube runtime.

### PR29 — Versioned publication declarations contract v2
Backward-compatible v1 digest preservation plus strict human-approved child-directed and realistic altered/synthetic-media declarations. V2 declarations participate in exact approval/idempotency identity, map to YouTube publication status, and are verified after upload.

Milestone exit condition achieved: an authenticated final render can become one exact human-approved YouTube publication with durable evidence and duplicate-publication safety.

---

## PR30 — Roadmap v2 / post-PR29 status reconciliation

Status: **complete**.

PR30 reconciled the repository with actual implementation through PR29. Analytics was initially selected as the next architecture step. Re-evaluation against the original phone-first workflow showed a more immediate bottleneck: Content Forge already had a strong production engine but still exposed too much engineering topology for daily use.

That finding changed priority, not architecture. Analytics remains planned; Daily Production Completion comes first.

---

## Milestone 7B — Daily Production Completion

Status: **current product phase**.

Goal: make routine short-form production possible from the phone without exposing internal subsystem topology. The desktop remains the worker; existing Project/Review/Render/Publishing contracts remain authoritative.

### PR31 — Phone production home and daily-use flow

Status: **complete**.

Delivered:

- primary project-centric Production Home in the existing PWA;
- human-readable recent/active project cards rather than internal IDs;
- bounded Inbox / Attention / Rendering / Ready projection from existing state;
- `Start video` reuses existing review bootstrap authority;
- `Continue` reuses the existing phone review surface;
- `Render final` reuses exact approved-preview final-render authority;
- `Watch final` uses the authenticated final artifact endpoint;
- advanced Dialogue / Voice Cast / Voiced Story / Scene Presentation / Profiles / Library / Publishing surfaces remain available behind `Advanced`;
- installed PWA shell upgraded to v17 without creating a second product state machine.

PR31 deliberately used the already-proven `hook_overlay` flow as the first everyday happy path instead of pretending every registered format already had equally ergonomic phone controls.

### PR32 — Phone create-video wizard and human-facing presets

Status: **complete in the intended post-merge state**.

Delivered:

- a real `Create video` entry point on Production Home;
- human-facing presets backed by existing exact template versions:
  - **Hook Short** → `hook_overlay@1.0`;
  - **Top Bar Short** → `hook_topbar@1.0`;
  - **Framed Clip** → `content_frame@1.0`;
  - **Art Story** → `art_story@1.0`;
  - **Panel Story** → `panel_sequence@1.0`;
- authoritative source catalog from canonical Inbox Projects and probed Asset metadata rather than client MIME hints;
- explicit multi-source selection and phone-controlled scene ordering;
- image-only enforcement for Art Story / Panel Story;
- early rejection of incomplete video probe metadata and unusable required Art Story credit;
- canonical UUID request identity mapped to one deterministic production Project ID;
- idempotent same-request replay and fail-closed conflicting UUID reuse;
- provenance-preserving derived production Projects rather than mutating Inbox source Projects into compound productions;
- canonical scalar `production_preset_v1` evidence that pins exact preset/template plus ordered `{source_project_id, asset_id, source_id}` snapshots;
- validation that source refs, provenance records, and actual scene order still match the original wizard choice after reload/restart;
- tampered preset Projects quarantined from the trusted production-project catalog;
- preset-aware bootstrap into the existing PR10 Review lifecycle;
- no redundant `source_order` review task because ordering is already explicit human authority in the wizard;
- generic registered-template preview/final compilation while retaining existing voiced-story and PR23/PR24 render-authority guards;
- derived production Projects remain visible on Home after leaving Inbox or reaching `DONE`;
- installed PWA shell upgraded v17 → v18 with historical cache predecessors retained explicitly.

Exit condition achieved: prepared phone-visible media can become an intentionally selected production format without knowing template IDs or creating a parallel renderer/review model.

See [`docs/pr32-phone-create-video-presets.md`](docs/pr32-phone-create-video-presets.md) for the exact authority contract.

### PR33 — Project-specific edit, preview, and final happy path

Status: **complete in the intended post-merge state**.

Delivered:

- one coherent project-specific phone surface from bounded review through final playback;
- Production Home `Start / Continue / View progress / View final` actions open that exact Project context;
- PR32-created Projects open directly after creation instead of dropping into the global Review queue;
- Hook editing through the existing exact review-task resolve authority;
- bounded per-scene crop editing over the existing `NormalizedRect` contract, including full-frame/null semantics and mobile 2×2 controls;
- optional title/description/hashtags through the existing metadata review task;
- PR32 source order retained as read-only human authority rather than duplicated by a second reorder path;
- authenticated preview generation and inline playback;
- exact preview approve/reject-and-edit actions without leaving the Project context;
- `READY` remains intentionally editable under existing core semantics: an edit invalidates the approved preview and returns to review;
- `RENDERING` / `QC` / `DONE` never expose false review mutations; historically open optional tasks are shown as locked history;
- final render progress and authenticated final playback remain on the same Project surface;
- global Capture / Review and specialist engineering panels remain available behind `Advanced` as fallback/debug controls;
- no new Project endpoint, state machine, render authority, filesystem exposure, provider dependency, or publishing side effect;
- installed PWA shell upgraded v18 → v19 while retaining v18 as an explicit predecessor.

Exit condition achieved: common PR32 Short / Framed / Story Projects can move through relevant decisions, authenticated preview, approval/rejection, final render/QC, and final playback from one coherent phone context without routine use of project IDs, render job IDs, or the global Review surface.

See [`docs/pr33-project-specific-phone-flow.md`](docs/pr33-project-specific-phone-flow.md) for the exact projection and lifecycle contract.

### PR34 — Final-to-publish phone handoff

Status: **complete in the intended post-merge state**.

Delivered:

- a Project-specific Publish stage appears only for canonical `DONE` Projects with exact final job/hash evidence;
- the routine phone path carries exact `project.final.job_id` into the existing publishing candidate boundary automatically;
- no manual Project, render-job, provider, destination/channel, or publish-attempt IDs are required in the ordinary path;
- configured providers may expose only a credential-free `PublishTarget`; the YouTube adapter exposes `youtube` plus the configured channel ID while OAuth/token paths remain provider-local;
- unknown providers are never guessed and fall back to Advanced publishing or local-only final use;
- title/description/tags remain editable publish metadata, with visibility and optional schedule retained as explicit choices;
- PR29 child-directed and realistic altered/synthetic-media declarations remain mandatory explicit Yes/No human authority;
- exact candidate construction, semantic SHA-256, idempotency identity, exact approval, durable attempt storage, provider preflight, execution, and receipt validation remain the existing PR27–PR29 contracts;
- publish approval and remote execution remain separate explicit actions;
- authenticated read-only project publishing projection filters existing ledger history by exact `{project_id, render_job_id, output_sha256}` rather than adding a second ledger;
- projection results are safety-prioritized before any bounded result limit: `outcome_unknown > running > succeeded > prepared > failed`, with newest-first ordering within the same state;
- the phone bundle independently verifies returned project/job/hash identity and uses the same conservative state priority;
- `outcome_unknown` and `running` block routine replacement; `succeeded` does not trigger a duplicate upload; retry-safe pre-boundary `failed` may form a new exact request;
- multiple ambiguous active attempts fail closed into Advanced inspection rather than choosing an attempt heuristically;
- refresh/reopen reads the durable ledger after existing reconciliation, so browser state is never publication authority;
- Advanced publishing remains available as fallback/debug/recovery surface;
- provider-free render/QC/playback/export remains usable;
- installed PWA shell upgraded v19 → v20 while retaining v19 as an explicit predecessor.

Exit condition achieved: an approved final video can enter the existing safe publish flow from the same phone Project context without exposing publishing ledger internals or routine IDs, while retaining PR27–PR29 exact authority and duplicate-publication safety.

See [`docs/pr34-final-to-publish-phone-handoff.md`](docs/pr34-final-to-publish-phone-handoff.md) for the exact projection, recovery, and authority contract.

### PR35 — Mobile batch Inbox and attention queue

Planned.

- practical daily summary such as ready automatically / needs attention / rendering / failed;
- batch deterministic preparation without babysitting individual projects;
- attention queue grouped into small contextual decisions;
- retry/recovery actions only where existing operation semantics say they are safe;
- no hidden automatic acceptance of review authority.

Exit condition: multiple pieces of source material can move through the production pipeline with human attention spent only on actual blockers.

Milestone 7B exit condition:

```text
phone share/upload
-> choose a useful format
-> make bounded project decisions
-> preview
-> approve
-> desktop final render/QC
-> inspect final on phone
-> publish
```

for the common production paths, without requiring routine access to desktop UI, CLI commands, project IDs, render job IDs, template versions, or subsystem-specific engineering panels.

---

## Milestone 8 — Measurement, experiments, and evidence-driven improvement

Status: **planned after Daily Production Completion**.

The objective is not to build a generic prediction engine that claims to know what will perform well. Content Forge should first retain trustworthy observations from the user's own published work, then make bounded recommendations traceable to that evidence.

### PR36 — Analytics provider boundary

Planned.

- replaceable `AnalyticsProvider` independent from publishing providers;
- typed observations with provider/version evidence;
- exact link to durable successful publication identity;
- observation time/window semantics distinct from ingestion time;
- additive history-oriented storage;
- provider-free Content Forge remains usable.

### PR37 — YouTube Analytics adapter

Planned.

- optional authenticated YouTube Analytics/Data implementation;
- ingest exact known published video IDs first;
- normalized supported performance metrics with retained provider evidence;
- bounded partial/unavailable behavior.

### PR38 — Durable performance history and observation windows

Planned.

- history-preserving observation snapshots;
- explicit comparable windows where provider data permits;
- provisional versus mature measurements;
- deterministic summaries and explicit missing/late data.

### PR39 — Experiment identity and publication attribution

Planned.

- immutable experiment definitions over already-represented production decisions;
- exact attribution from production choice to render and publish receipt;
- no automatic causal claims from uncontrolled comparisons.

### PR40 — Performance dashboard and comparison PWA

Planned.

- publication timeline and metric-window views;
- comparisons across supported production dimensions;
- visible sample sizes and missing/partial/stale state;
- no hidden ranking objective.

### PR41 — Recommendation assistance from owned historical evidence

Planned only after PR36–PR40 produce trustworthy evidence.

- bounded recommendations from retained production/performance history;
- traceable supporting observations;
- uncertainty/sample-size limitations first-class;
- proposals never mutate projects or publishing choices without human review.

Milestone 8 target loop:

```text
exact production choice
-> exact render
-> exact publication
-> authenticated observations over time
-> comparable retained history
-> bounded recommendation
-> human decision
```

---

## Later convenience integrations and operational reach

Useful, but not blockers for Daily Production Completion or the evidence loop.

### Source-specific import helpers

Candidate scope:

- source-specific helpers only where APIs/terms permit;
- preserve original source URL/creator/title/permission evidence;
- no broad scraping architecture;
- imported bytes still enter through canonical local asset/provenance boundaries.

### Private remote access profile

Candidate scope:

- documented/tested private-network access such as Tailscale-style deployment;
- retain TLS/auth/session boundaries;
- no public-internet exposure by default;
- no cloud dependency in the core production runtime.

### Later candidates — only when justified by real use

- second publishing platform adapter;
- YouTube thumbnails/captions/playlists or additional approved publication semantics;
- durable long-running remote processing state if real long-form publishing proves the current bounded policy too conservative;
- historical channel analytics import separate from Content Forge-owned publications;
- more formats/components driven by actual workflow demand.

These items should not be implemented merely to make the feature list longer.

---

## Architectural invariants across all milestones

1. Raw production media is local runtime data, not repository content.
2. `ContentKind`, `Template`, and `Workflow` remain separate.
3. The renderer consumes a normalized timeline/render plan, not business-specific content types.
4. Providers are replaceable and optional.
5. Human-required decisions surface as explicit review tasks or exact approvals.
6. Every output can be traced back to source records and accepted project state.
7. New content formats should normally be additive.
8. Short-form and long-form share the same scene/timeline runtime.
9. Fast previews and final renders are separate output profiles over the same project.
10. Publishing authority is separate from render correctness and project state.
11. Credentials and machine-local paths stay outside semantic request/evidence identities unless the identity specifically describes local storage itself.
12. Uncertain remote side effects fail closed; automatic duplicate publication is never preferred over explicit reconciliation.
13. Daily-use UI is a projection of canonical Project/Review/Render/Publishing authority, not a second state machine.
14. Routine phone UX should not expose internal IDs, template versions, or subsystem topology when the workflow can derive them safely.
15. A phone production preset is product vocabulary over exact registered template authority, not a second template system.
16. Explicit wizard source selection/order is durable human authority and must remain traceable to exact asset/source provenance.
17. Project-specific phone editability is derived from canonical lifecycle state; `RENDERING`, `QC`, and `DONE` never expose review mutations.
18. Routine project publishing must be bound to the exact final `{project_id, render_job_id, output_sha256}` and bounded projections must preserve more safety-significant remote states before convenience ordering.
19. Analytics observations are evidence, not authority to mutate production state.
20. Recommendations remain proposals and must be traceable to retained evidence.
21. The project optimizes human attention and trustworthy evidence before it optimizes machine cleverness.
