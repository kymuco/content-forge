# Roadmap

This roadmap is intentionally organized as small reviewable pull requests. The architecture should become useful early and remain replaceable at the edges: providers, templates, components, and workflows can evolve without destabilizing the project model or renderer core.

## Milestone 0 — Foundation

### PR1 — Architecture, taxonomy, workflow, and v0.1 contract

Status: **in progress**

Deliverables:

- product vision and boundaries;
- content-kind / template / workflow separation;
- scene/timeline architecture;
- phone-first ingest and review model;
- provider boundaries for LLM/OCR/TTS/source integrations;
- rendering responsibilities and QC model;
- source provenance and repository hygiene rules;
- concrete v0.1 vertical-slice specification;
- staged implementation roadmap.

Exit condition: implementation can begin without inventing fundamental concepts inside the first renderer PR.

---

## Milestone 1 — Core runtime and first useful vertical slice

### PR2 — Python package bootstrap and core domain contracts

Build the minimal project skeleton and immutable/validated domain models.

Deliverables:

- `pyproject.toml`;
- `src/content_forge/` package;
- Pydantic models for `Project`, `AssetRef`, `SourceRecord`, `Variant`, `Scene`, `Overlay`, `AudioTrack`, `OutputProfile`, and `ReviewTask`;
- stable IDs and schema version fields;
- JSON/YAML serialization round trips;
- pytest baseline and CI;
- no rendering yet.

Exit condition: a project manifest can be created, validated, saved, and loaded without loss.

### PR3 — Local asset store, hashing, SQLite metadata, and provenance

Deliverables:

- content-addressed local asset storage;
- SHA-256 hashing and deduplication;
- SQLite project/asset/job metadata;
- source URL, platform, creator/artist, source title, acquisition time, credit and permission notes;
- thumbnails/proxy metadata slots;
- explicit separation between repository files and local runtime data;
- tests for duplicate ingest and provenance retention.

Exit condition: arbitrary local media can enter the library once and be referenced safely by multiple projects.

### PR4 — Scene/timeline model and deterministic timeline compiler

Deliverables:

- ordered scene graph;
- media placement, timing, crop/fit, simple motion, transitions, overlays, and audio timing;
- deterministic conversion from project + template to a normalized render plan;
- validation for overlaps, missing sources, negative/invalid durations, and output bounds;
- snapshot tests for render plans.

Exit condition: templates can produce a renderer-independent timeline plan.

### PR5 — FFmpeg/ffprobe backend and capability probing

Deliverables:

- ffprobe metadata extraction;
- FFmpeg command/filtergraph compiler;
- scale/crop/pad/overlay/text/audio primitives;
- NVENC capability detection with CPU fallback;
- render cancellation and structured errors;
- deterministic command manifests;
- synthetic test fixtures only.

Exit condition: a normalized render plan can render a basic image/video composition to MP4.

### PR6 — First template: `hook_overlay`

This is the first complete content format and the first real output target.

Deliverables:

- `hook_overlay` template;
- video/image source fit modes;
- top-safe-zone headline placement;
- font wrapping and stroke/background options;
- YouTube Shorts 1080x1920 output profile;
- audio passthrough/normalization baseline;
- golden/snapshot fixtures.

Exit condition: a local clip + headline produces a correct 1080x1920 Short.

### PR7 — Preview/final render orchestration and artifact manifests

PR6 proved the first complete render path. Before exposing that path through a phone-facing service, freeze one durable render-attempt contract so API, PWA, and later batch workers do not invoke FFmpeg directly or invent incompatible artifact metadata.

Deliverables:

- persistent render jobs backed by the existing SQLite job table;
- immutable `RenderPlan` snapshots per submitted attempt;
- explicit preview/final purpose bound to output-profile identity;
- atomic queued-to-running job claims and terminal states;
- runtime-relative artifact storage under project/job identity;
- successful artifact sidecars with source, plan, command, output, encoder, and ffprobe fingerprints;
- structured failed/cancelled sidecars;
- output hash/dimension verification before success is published;
- real FFmpeg integration coverage through the persisted job boundary;
- no worker pool, batch scheduler, API/PWA surface, or publishing yet.

Exit condition: a persisted preview/final render attempt can be executed and recovered as a verified MP4 plus reproducibility sidecar without depending on an in-memory project state.

---

## Milestone 2 — Phone-first production workflow

### PR8 — FastAPI service and local Inbox

Deliverables:

- FastAPI server;
- multipart upload from phone/desktop;
- URL/note intake records even when downloading is not automated;
- automatic asset ingest, ffprobe, thumbnail generation, and draft-project creation;
- local-network access controls;
- no public-internet exposure by default.

Exit condition: media sent over Wi-Fi reaches the desktop library without USB or manual folder work.

### PR9 — PWA shell and share-to-Inbox flow

Deliverables:

- responsive mobile-first web UI;
- installable PWA;
- Android share target where supported;
- Inbox list and project cards;
- upload progress and failure recovery;
- QR/local address onboarding.

Exit condition: normal flow on Android is `Share -> Content Forge -> Inbox`.

### PR10 — Review queue and proxy preview

Deliverables:

- explicit `AUTO`, `REVIEW`, `MANUAL` task semantics;
- review queue ranked by blocking status;
- 540x960 fast preview profile;
- approve/reject/edit loop;
- phone controls for hook selection, crop confirmation, ordering, and simple metadata;
- project state machine from Inbox through Done.

Exit condition: a project can be completed without sitting at the desktop unless a genuinely complex edit is needed.

---

## Milestone 3 — Template/component system and initial format coverage

### PR11 — Template registry, skins, slots, and component contracts

Deliverables:

- declarative template schema;
- component registry;
- slots, anchors, safe zones, defaults, and per-template validation;
- template and component versioning;
- reusable skins/assets with redistribution-safe fixtures;
- plugin discovery boundary without third-party plugin loading yet.

Exit condition: adding a simple visual format no longer requires changes to core timeline or renderer code.

### PR12 — Initial template pack

Add the formats already identified during research:

- `hook_topbar`;
- `social_post`;
- `meme_white_header`;
- `anime_frame` / generic `content_frame`;
- `art_story`;
- `panel_sequence`;
- `sync_stack`;
- `reaction_bottom` composition support.

Exit condition: all initial non-voiced content families can be represented using the same runtime.

### PR13 — Reusable overlay and motion components

Deliverables:

- `ArtistCredit`;
- `CommentCard`;
- `Reaction` (PNG/GIF/WebM/MP4 loop where backend permits);
- `Avatar`;
- `Watermark`;
- `KenBurns`/slow zoom;
- pan/crop reveal;
- blur reveal;
- simple transition set;
- automatic text overflow checks.

Exit condition: art/manga/meme formats can be composed from reusable components rather than bespoke scripts.

### PR14 — Music and audio composition

Deliverables:

- music library references;
- original audio/music mix controls;
- fade/duck/normalize;
- loudness/peak QC baseline;
- per-template audio policy;
- cached audio intermediates.

Exit condition: batch outputs have predictable audio without manual FFmpeg work.

---

## Milestone 4 — Optional intelligence and variants

### PR15 — LLM provider boundary and `chatgpt-web-adapter`

Deliverables:

- `LLMProvider` protocol;
- adapter implementation using `chatgpt-web-adapter`;
- hook suggestions;
- title/description suggestions;
- OCR-text cleanup helper contract;
- translation helper;
- content-kind/template suggestions;
- every generated value remains proposed until accepted where judgment matters;
- renderer works identically with the provider disabled.

Exit condition: language/semantic assistance is useful but never a hard dependency.

### PR16 — Language variants and localized render metadata

Deliverables:

- master project + language variants;
- localized hook, subtitle, title, description, hashtags, and optional font selection;
- shared source/timeline references;
- variant-specific preview/render cache keys;
- EN/JA/KO examples using synthetic text fixtures.

Exit condition: one source project can produce multiple language variants without duplication of media or timelines.

### PR17 — Batch preparation, render queue, QC, and reproducibility

Deliverables:

- persistent SQLite render/job queue building on the PR7 render-attempt contract;
- crash-safe resume;
- batch preview/final render;
- source-hash, template-version, renderer-version, accepted-text, and provider-parameter manifest;
- QC for duration, dimensions, missing audio/assets, overflow, safe zones, black frames where practical, and render failures;
- export sidecar metadata.

Exit condition: v0.1 can reliably process a batch of projects end-to-end.

### v0.1 release boundary

v0.1 is complete when the following flow is boring and reliable:

```text
Phone discovery
-> Share/upload over Wi-Fi
-> Inbox
-> automatic ingest/probe
-> select or accept template
-> review small decisions on phone
-> fast preview
-> final NVENC render
-> QC
-> MP4 + metadata export
```

v0.1 intentionally excludes automatic publishing, broad web scraping, full OCR automation, and voiced-story production.

---

## Milestone 5 — Voiced panels and persistent cast

### PR18 — OCR provider and panel text extraction workflow

Deliverables:

- `OCRProvider` protocol;
- one local implementation selected after evaluation;
- bounding boxes + confidence;
- review tasks for uncertain text;
- original OCR output retained alongside corrected text;
- no automatic speaker guessing required for release.

### PR19 — Dialogue scene model and speaker assignment

Deliverables:

- dialogue lines attached to panels/scenes;
- ordered reading flow;
- speaker IDs;
- character registry;
- manual/assisted speaker assignment UI;
- scene focus hints (`speaker`, `face`, explicit crop).

### PR20 — TTS provider and Qwen TTS integration

Deliverables:

- `TTSProvider` protocol;
- local Qwen TTS implementation;
- per-line generation and caching;
- voice/style parameters in manifests;
- duration extraction;
- deterministic cache invalidation.

### PR21 — Voice Cast registry

Deliverables:

- persistent cast voices such as protagonist, secondary characters, narrator;
- character-to-cast mapping;
- preview voice button from phone/desktop;
- project overrides without mutating global cast;
- channel/profile-specific casts later.

### PR22 — Voiced story review UI and timed text

Deliverables:

- panel-centric editor instead of a generic NLE timeline;
- OCR correction;
- speaker and voice assignment;
- listen/regenerate controls;
- automatic scene duration from synthesized lines;
- phrase-level timed text/ASS rendering;
- optional forced alignment left for a later PR.

### PR23 — Voiced scene audio mix and camera choreography

Deliverables:

- dialogue sequencing;
- ambience/music ducking;
- pause rules;
- pan/zoom/focus changes around speakers;
- reusable scene presets;
- preview and QC for missing/overlapping dialogue.

Exit condition: a panel sequence can become a polished voiced Short with only bounded review tasks.

---

## Milestone 6 — Long-form and reusable production assets

### PR24 — Long-form output profiles

Deliverables:

- 16:9 1080p/1440p profiles;
- chapter/scene concatenation;
- shared voiced scenes between Short and long-form projects;
- long-form render caching;
- no separate renderer architecture.

### PR25 — Project/series/channel profiles

Deliverables:

- reusable branding;
- default templates and skins;
- default voice cast;
- language defaults;
- credit policy;
- output profiles;
- music/reaction libraries.

### PR26 — Production library search and tagging

Deliverables:

- game/anime/artist/character/topic/source tags;
- fast local search;
- virtual collections rather than requiring manual physical folders;
- duplicate and previously-used warnings;
- source reuse history.

---

## Milestone 7 — Source adapters, publishing, and analytics

These are deliberately postponed until the production loop is proven.

Potential work:

- source-specific import helpers where terms and APIs allow;
- remote access through a private network such as Tailscale;
- publishing provider boundary;
- YouTube upload/scheduling integration;
- analytics ingest;
- template/hook experiment tracking;
- viewed-vs-swiped, retention, subscriber conversion, restrictions, and revenue-oriented evaluation;
- recommendation assistance based on the user's own historical performance.

Automatic publishing must not become a prerequisite for rendering or exporting.

---

## Architectural invariants across all milestones

1. Raw production media is local runtime data, not repository content.
2. `ContentKind`, `Template`, and `Workflow` remain separate.
3. The renderer consumes a normalized timeline/render plan, not business-specific content types.
4. Providers are replaceable and optional.
5. Human-required decisions surface as explicit review tasks.
6. Every output can be traced back to source records and accepted project state.
7. New content formats should normally be additive.
8. Short-form and long-form share the same scene/timeline runtime.
9. Fast previews and final renders are separate output profiles over the same project.
10. The project optimizes human attention before it optimizes machine cleverness.
