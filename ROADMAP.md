# Roadmap

This roadmap is organized as small reviewable pull requests. The architecture should become useful early and remain replaceable at the edges: providers, templates, components, publishing adapters, and analytics integrations can evolve without destabilizing the canonical project model or renderer core.

## Current implementation status

- PR1–PR30: **complete** in the intended post-merge repository state.
- Current product step: **PR31 — Analytics provider boundary**.
- Milestones 0–6: **complete**.
- Milestone 7A — Publishing: **complete through the first production YouTube path**.
- Milestone 7B — Measurement, experiments, and evidence-driven improvement: **next**.
- The intended **v0.1 batch-production boundary remains complete through PR17**; later PRs extend the same runtime rather than replacing it.

The current product loop is already durable through publication:

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

The next roadmap phase closes the feedback loop:

```text
Source
-> Prepare
-> Review
-> Render
-> Publish
-> Measure
-> Compare
-> Recommend
-> Next production decision
   ^_____________________|
```

The objective is not to build a generic prediction engine that claims to know what will perform well. Content Forge should first retain trustworthy observations from the user's own published work, then make bounded recommendations that are traceable to that evidence.

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

## Milestone 2 — Phone-first production workflow

Status: **complete**.

### PR8 — Authenticated local FastAPI, durable Inbox ingest, and media preparation

Authenticated local ingest, durable staging/recovery, content-addressed acceptance, probing/thumbnails, Inbox project creation, single-owner runtime, and secure LAN boundary.

### PR9 — PWA shell and share-to-Inbox flow

Installable phone-first PWA, Android share target, Inbox UI, upload recovery, and pairing/onboarding.

### PR10 — Review queue and proxy preview

Explicit review authority, fast previews, approve/reject/edit loop, phone controls, and final-production lifecycle.

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

Automatic publishing, voiced stories, and analytics were intentionally outside the v0.1 boundary and were added later without replacing the v0.1 renderer/runtime.

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

Status: **complete**.

Persistent reusable cast identity, character-to-cast bindings, immutable cast revisions, project-local overrides, preview synthesis, and exact reference-audio integrity.

### PR22 — Voiced story review UI and timed text

Status: **complete**.

Panel-centric voiced-story review, listen/regenerate controls, verified audio-derived scene timing, phrase-level editorial timed text, reversible materialization, and render guards.

### PR23 — Voiced scene audio mix and camera choreography

Status: **complete**.

Dialogue/presentation QC, music/ambience ducking, reversible camera choreography, focus intent, preview lifecycle invalidation, and real FFmpeg integration.

Milestone exit condition achieved: a panel sequence can become a reviewed, voiced, timed, mixed, camera-presented render without introducing a second timeline or renderer authority.

---

## Milestone 6 — Long-form and reusable production assets

Status: **complete**.

### PR24 — Long-form output profiles

Status: **complete**.

16:9 1080p/1440p output through the existing timeline/render architecture, chapter metadata, authenticated render reuse, and exact cross-project shared voiced-scene references.

### PR25 — Project / series / channel production profiles

Status: **complete**.

Revisioned reusable production defaults for branding, templates/skins, cast, languages, credit policy, output profiles, music/reaction libraries, with explicit reversible Project binding.

### PR26 — Production library search and tagging

Status: **complete**.

Indexed bounded tags, Unicode-safe search/prefix search, virtual collections, duplicate lookup, source-reuse history, used/unused filters, authenticated API, and PWA library surface.

---

## Milestone 7A — Publishing and exact remote authority

Status: **complete through the first YouTube production path**.

Publishing remains optional. Rendering/export is always usable without a publishing provider.

### PR27 — Publishing provider boundary and export-to-publish handoff

Status: **complete**.

Platform-agnostic credential-free `PublishRequest`, exact human approval, semantic/idempotency identity, durable publish ledger, replaceable provider boundary, explicit remote side-effect boundary, and fail-closed `outcome_unknown` semantics.

### PR28 — YouTube Data API publishing adapter

Status: **complete**.

Installed-app OAuth, exact channel binding, authenticated immutable media snapshot, resumable YouTube upload/scheduling, processing verification, exact remote metadata verification, provider-local secret boundary, and optional YouTube runtime.

### PR29 — Versioned publication declarations contract v2

Status: **complete**.

Backward-compatible v1 digest preservation plus strict human-approved child-directed and realistic altered/synthetic-media declarations. V2 declarations participate in exact approval/idempotency identity, map to YouTube publication status, and are verified after upload.

Milestone exit condition achieved: an authenticated final render can become one exact human-approved YouTube publication with durable evidence and duplicate-publication safety.

---

## PR30 — Roadmap v2 / post-PR29 status reconciliation

Status: **complete**.

Delivered:

- reconciled README/ROADMAP with the actual merged PR1–PR29 implementation state;
- marked Milestones 5 and 6 complete;
- split Milestone 7 into completed publishing work and the next measurement/learning phase;
- made PR31 the next product implementation step;
- moved convenience integrations below the evidence feedback loop rather than treating them as blockers;
- reconciled provider documentation so the already-implemented publishing boundary is no longer described as future work.

PR30 changes documentation only; it introduces no runtime, schema, provider implementation, API, PWA, storage, or rendering behavior.

---

## Milestone 7B — Measurement, experiments, and evidence-driven improvement

Status: **next**.

This is the next primary product phase.

### PR31 — Analytics provider boundary

Current planned product step.

Deliverables:

- replaceable `AnalyticsProvider` protocol independent from publishing providers;
- typed metric/observation contracts with explicit provider/version evidence;
- exact link from an observation to a durable successful `PublishResult` / remote publication identity;
- observation time and metric window semantics separated from ingestion time;
- no credentials, filesystem paths, or provider sessions in semantic analytics records;
- additive durable storage designed for repeated observations rather than overwriting one mutable current value;
- explicit unavailable/partial/provider-error behavior;
- provider-free Content Forge remains fully usable.

Exit condition: Content Forge can represent trustworthy performance observations without coupling the core model to YouTube-specific metric names or OAuth behavior.

### PR32 — YouTube Analytics adapter

Planned.

Deliverables:

- optional YouTube Analytics/Data API implementation behind PR31;
- account/channel identity bound to the already configured publication destination;
- ingest only for exact known published remote video IDs unless explicitly importing historical channel data in a later feature;
- normalize supported views/watch/engagement/subscriber/restriction metrics into PR31 observations while retaining raw provider evidence where useful;
- bounded pagination/rate handling and controlled partial availability;
- OAuth scopes remain local provider configuration, not analytics identity.

Exit condition: a Content Forge YouTube publication can acquire authenticated performance observations through a replaceable adapter.

### PR33 — Durable performance history and observation windows

Planned.

Deliverables:

- append-only or otherwise history-preserving observation snapshots;
- explicit comparable windows such as early/24h/7d/30d where provider data permits;
- distinguish provisional from mature measurements;
- no silent replacement of historical values with the latest fetch;
- deterministic derived summaries over retained observations;
- missing-data and late-data behavior remains explicit.

Exit condition: the system can answer how a publication evolved over time, not merely what its counters say now.

### PR34 — Experiment identity and publication attribution

Planned.

Deliverables:

- versioned experiment definitions over production decisions already represented by Content Forge;
- exact attribution from accepted variant/template/hook/language/presentation choices to final render and publish receipt;
- experiment arms are immutable evidence, not mutable labels attached after results are known;
- no automatic causal claims from uncontrolled comparisons;
- support ordinary production history even when no experiment was declared.

Exit condition: Content Forge can say exactly what production choice was being tested when a published result was produced.

### PR35 — Performance dashboard and comparison PWA

Planned.

Deliverables:

- publication timeline and metric-window views;
- comparisons across templates, hooks, languages, profiles, formats, and declared experiment arms where evidence supports them;
- source counts/sample sizes shown alongside aggregates;
- missing/partial/stale analytics visibly distinguished from zero;
- no hidden ranking objective that silently changes production decisions.

Exit condition: the user can inspect what happened and compare relevant historical outputs without querying provider dashboards manually.

### PR36 — Recommendation assistance from owned historical evidence

Planned only after PR31–PR35 produce trustworthy evidence.

Deliverables:

- bounded recommendations over the user's own retained production/performance history;
- every recommendation cites the observations/comparisons that motivated it;
- uncertainty/sample-size limitations are first-class;
- proposals never mutate projects or publishing choices without normal human review;
- optional LLM assistance may explain/summarize evidence but does not become metric authority;
- avoid promises that a recommendation will improve reach, retention, revenue, or virality.

Exit condition: Content Forge can propose the next production decision because of traceable owned evidence, while the human retains judgment authority.

Milestone 7B target loop:

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

## Milestone 8 — Convenience integrations and operational reach

These are useful but are intentionally not blockers for the measurement feedback loop.

### PR37 — Source-specific import helpers

Candidate scope:

- source-specific helpers only where APIs/terms permit;
- preserve original source URL/creator/title/permission evidence;
- no broad scraping architecture;
- imported bytes still enter through canonical local asset/provenance boundaries.

### PR38 — Private remote access profile

Candidate scope:

- documented and tested private-network access such as Tailscale-style deployment;
- retain TLS/auth/session boundaries;
- no public-internet exposure by default;
- no cloud dependency in the core production runtime.

### Later candidates — only when justified by real use

- second publishing platform adapter;
- YouTube thumbnails/captions/playlists or additional approved publication semantics;
- a durable long-running remote `processing` state if bounded YouTube processing proves too conservative in real long-form use;
- historical channel analytics import separate from Content Forge-owned publications;
- more production formats/components driven by actual workflow demand.

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
13. Analytics observations are evidence, not authority to mutate production state.
14. Recommendations remain proposals and must be traceable to retained evidence.
15. The project optimizes human attention and trustworthy evidence before it optimizes machine cleverness.
