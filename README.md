# content-forge

Local-first modular content production system for short-form and long-form media.

`content-forge` is designed around a simple workflow: collect source material quickly from a phone, turn it into structured projects, review only the decisions that need human attention, and let the desktop worker handle rendering, audio, previews, QC, export, and optional human-approved publishing.

The project starts with YouTube Shorts-style workflows, but the core model is intentionally platform- and format-agnostic. The same scene/timeline runtime produces short vertical edits, art stories, panel sequences, voiced comic/manhwa scenes, and long-form video.

## Core principles

- **Local-first.** Source libraries, generated media, credentials, and production state stay on the user's machine by default.
- **Phone-first ingest and review.** Finding material and approving small decisions should not require sitting at the desktop.
- **Content kind, presentation template, and workflow are separate concepts.** A character moment can use a clean hook, a meme layout, a frame, or a synchronized stack without changing the source model.
- **One scene/timeline runtime.** Video, images, image sequences, text, reactions, subtitles, TTS, music, transitions, short-form, and long-form compile into the same render architecture.
- **Providers are optional boundaries, not infrastructure.** LLM, OCR, TTS, publishing, and future analytics integrations remain replaceable at the edges.
- **Human attention is explicit.** Automation proceeds until a real judgment is needed, then creates a bounded review task or exact approval instead of silently guessing.
- **Reproducible output.** Projects retain source hashes, accepted text, exact template/component versions and definition evidence, provider parameters, render metadata, and publication evidence.
- **Extensible by composition.** New content formats should usually require a template, component, provider, or workflow plugin—not changes to the core renderer.
- **Remote side effects fail closed.** Rendering/export remains independent from publishing, and uncertain publication outcomes are never treated as safe automatic retries.

## Current architecture

```text
Phone / Desktop
      |
 authenticated local API
      |
+-----+------------------+
|                        |
Inbox                Review Queue
|                        |
+-----------+------------+
            |
   phone preset selection
            |
          Project
            |
   Sources / Variants
            |
          Template
            |
 versioned registry
            |
          Timeline
   +--------+--------+
   |        |        |
 Scenes  Overlays  Audio
   +--------+--------+
            |
      Render Compiler
            |
          FFmpeg
            |
     durable render job
            |
       Batch / QC
            |
 reproducibility + export
            |
 exact publish approval
            |
 optional PublishingProvider
            |
          YouTube
```

The current product-completion phase makes the original asymmetric workflow pleasant to use every day:

```text
Phone: Share / Create video / choose format + sources / bounded decisions / approve
                                      |
                                      v
Desktop: ingest / prepare / preview / render / QC / publish boundary
```

Analytics and evidence-driven recommendations remain a later feedback-loop phase after the daily production workflow is genuinely convenient.

## Development status

**PR1–PR33 are complete in the intended post-merge repository state.** Milestones 0–6 are complete, Milestone 7A includes the first production YouTube publishing path, PR30 reconciles the roadmap/provider documentation, PR31 adds the project-centric phone Production Home, PR32 adds the real phone Create video wizard over existing template/Review/Render authority, and PR33 keeps bounded review, preview approval/rejection, final progress, and final playback inside one Project-specific phone context.

The implemented system includes canonical domain/storage/provenance contracts, deterministic timeline compilation, generic FFmpeg rendering, durable preview/final jobs, authenticated Inbox ingest, phone-first PWA review, versioned template/component/skin registries, initial format coverage, reusable motion/audio components, optional LLM assistance, localized variants, batch/QC/reproducibility, retained OCR and dialogue authority, verified per-line TTS, persistent Voice Cast identity, voiced-story timed text, dialogue/music/ambience presentation, camera choreography, long-form 1080p/1440p output, reusable project/series/channel profiles, production-library search/tagging/reuse history, a platform-independent publishing ledger, authenticated YouTube upload/scheduling, versioned human-approved YouTube publication declarations, a daily-use mobile production projection, human-facing production presets that create provenance-preserving Projects, and a project-specific phone workflow that reuses existing Review/Preview/Final authority without introducing a second product state machine.

The current product direction is **Daily Production Completion**. PR33 now establishes `Production Home → Create video → choose/order media → one Project screen → bounded decisions → authenticated preview → approve/reject → final render/QC → final playback`. The next implementation step is **PR34 — Final-to-publish phone handoff**, followed by the mobile batch/attention work before analytics resumes. See [`ROADMAP.md`](ROADMAP.md) for the staged plan.

The original v0.1 vertical slice remains implemented through PR17:

```text
Phone upload/share
  -> Inbox
  -> Project
  -> registered template
  -> fast preview
  -> approval
  -> render
  -> batch/QC
  -> reproducibility + export
```

Later milestones extend that same runtime rather than creating a second production architecture.

## Local API

Install the project and run the API on loopback by default:

```text
content-forge-api
```

Plain HTTP is accepted only on loopback. Phone/LAN binding is intentionally fail-closed unless TLS is configured:

```text
content-forge-api --lan \
  --ssl-certfile /path/to/content-forge.crt \
  --ssl-keyfile /path/to/content-forge.key
```

The certificate must be trusted by the phone and valid for the hostname/IP used to connect. Bearer credentials are never sent over plaintext LAN HTTP; PR9 adds the current PWA/pairing/onboarding flow around this boundary.

Sensitive reads and writes require a paired bearer session. Pairing challenge creation additionally requires a loopback peer plus loopback `Host` and browser `Origin` (when present), closing the browser/DNS-rebinding bootstrap path. The API never returns raw runtime filesystem paths or stored token digests.

PR8 enforces one live API owner per runtime root with an OS advisory lock acquired before restart reconciliation. A second process using the same root is rejected while the first is alive; process termination or a crash releases ownership automatically so recovery can proceed without a stale timeout.

Accepted file bytes are identified by a durable exact size + SHA-256 receipt only after staging has been flushed/fsynced and its directory entry has been persisted where the platform supports that primitive. Post-acceptance operational filesystem or SQLite storage failures preserve resumable state instead of discarding the only verified copy. New canonical blobs and thumbnails also make their POSIX directory entries durable before the corresponding SQLite metadata receipt is committed.

The publishing runtime is optional. With no publishing provider configured, Content Forge still renders, exports, and can retain an approved `prepared` publication attempt without crossing a remote side-effect boundary. YouTube OAuth tokens remain explicitly local provider state and do not enter publish request identity, API/PWA payloads, or durable semantic evidence.

## Phone Create video presets

PR32 exposes product vocabulary instead of raw template/version identifiers:

- **Hook Short** — reviewed top hook over one or more image/video sources;
- **Top Bar Short** — dedicated reviewed top text area with media below;
- **Framed Clip** — image/video sources inside the existing safe vertical frame;
- **Art Story** — ordered still images with retained source-credit handling;
- **Panel Story** — ordered comic/manga/manhwa panels.

These are aliases over existing registered template authority, not a parallel renderer. The phone's selected source order is frozen into canonical PR32 evidence together with exact asset/source provenance, and the resulting Project immediately enters the existing Review → Preview → Approve → Final lifecycle.

## Phone project flow

PR33 keeps that lifecycle inside one coherent Project-specific phone surface. Production Home opens the exact Project rather than sending the user through a global engineering queue.

The Project screen exposes only already-modeled review authority:

- hook editing where a hook task exists;
- bounded full-frame or normalized per-scene crop editing where crop review exists;
- optional title/description/hashtags through the existing metadata task;
- PR32 source order as read-only retained authority;
- authenticated low-resolution preview generation/playback;
- exact preview approve or reject-and-edit actions;
- final render progress and authenticated final playback.

No new backend Project endpoint or state machine is introduced. `READY` remains editable according to existing core semantics: an edit invalidates the approved preview and returns the Project to review. `RENDERING`, `QC`, and `DONE` never offer review mutations; any historically open optional task is displayed as locked history.

Global Capture/Review and specialist production panels remain available behind `Advanced` for fallback/debug work, but ordinary Short / Framed / Story production no longer requires them.

## Initial content families

The production runtime currently covers:

- clean top-hook and top-bar layouts;
- social-post and meme-header presentation;
- generic framed content for anime/game/clip use cases;
- single-art and multi-art stories with bounded source-credit handling;
- comic, manga, and manhwa panel sequences;
- synchronized two/three-copy layouts;
- bottom reaction compositions with explicit reaction-asset provenance;
- reusable artist credits, comments, reactions, avatars, watermarks, pan/zoom/reveal motion, and simple transitions;
- deterministic original/music mixing with fades, timeline ducking, loudness/peak QC, and lossless premaster caching;
- localized variants over shared source/timeline state;
- OCR -> accepted dialogue -> speaker -> persistent cast -> TTS -> timed text -> voiced-scene mix/camera presentation;
- short vertical and 16:9 long-form output through the same timeline/render authority;
- reusable project/series/channel defaults and production-library search/tagging;
- optional exact human-approved YouTube upload and scheduling.

Analytics-driven comparison and recommendation are **not** implemented yet. They remain planned after the Daily Production Completion milestone, when real repeated use can produce trustworthy performance evidence and reveal which measurements are actually useful.

See [`docs/content-formats.md`](docs/content-formats.md) for the current taxonomy.

## Documentation

- [`ROADMAP.md`](ROADMAP.md) — staged implementation plan and current post-PR33 roadmap
- [`docs/vision.md`](docs/vision.md) — product goals and boundaries
- [`docs/architecture.md`](docs/architecture.md) — domain and runtime architecture
- [`docs/content-formats.md`](docs/content-formats.md) — content kinds, templates, and composition model
- [`docs/workflows.md`](docs/workflows.md) — project lifecycle and review model
- [`docs/phone-first-workflow.md`](docs/phone-first-workflow.md) — mobile ingest/review design
- [`docs/rendering-model.md`](docs/rendering-model.md) — timeline, render plan, FFmpeg/Pillow responsibilities
- [`docs/providers.md`](docs/providers.md) — provider architecture
- [`docs/safety-and-provenance.md`](docs/safety-and-provenance.md) — source tracking, credits, permissions, platform risk
- [`docs/v0.1-spec.md`](docs/v0.1-spec.md) — first implementation contract
- [`docs/pr8-local-api.md`](docs/pr8-local-api.md) — authenticated local API and Inbox contract
- [`docs/pr9-pwa.md`](docs/pr9-pwa.md) — PWA, Android share target, pairing, and phone Inbox flow
- [`docs/pr10-review-preview.md`](docs/pr10-review-preview.md) — review queue, proxy preview, approval, and production-decision contract
- [`docs/pr11-template-registry.md`](docs/pr11-template-registry.md) — exact-version template/component/skin registry and provenance contract
- [`docs/pr12-initial-template-pack.md`](docs/pr12-initial-template-pack.md) — registered initial non-voiced template pack and resolver boundaries
- [`docs/pr13-reusable-components.md`](docs/pr13-reusable-components.md) — reusable semantic overlay, motion, transition, and text-overflow contracts
- [`docs/pr14-music-audio.md`](docs/pr14-music-audio.md) — deterministic audio composition/mastering contracts
- [`docs/pr15-llm-provider.md`](docs/pr15-llm-provider.md) — optional LLM provider/proposal boundary
- [`docs/pr16-language-variants.md`](docs/pr16-language-variants.md) — localized variant/cache identity
- [`docs/pr17-batch-qc.md`](docs/pr17-batch-qc.md) — durable batch/QC/reproducibility
- [`docs/pr18-ocr-panel-text.md`](docs/pr18-ocr-panel-text.md) — retained OCR/correction authority
- [`docs/pr19-dialogue-scene-speaker-assignment.md`](docs/pr19-dialogue-scene-speaker-assignment.md) — dialogue/speaker authority
- [`docs/pr20-tts-qwen.md`](docs/pr20-tts-qwen.md) — durable TTS/Qwen integration
- [`docs/pr21-voice-cast.md`](docs/pr21-voice-cast.md) — persistent Voice Cast identity and project bindings
- [`docs/pr22-voiced-story-review-timed-text.md`](docs/pr22-voiced-story-review-timed-text.md) — voiced-story review, timing, and timed text
- [`docs/pr23-voiced-scene-mix-camera-choreography.md`](docs/pr23-voiced-scene-mix-camera-choreography.md) — voiced-scene mix and camera presentation
- [`docs/pr24-long-form-output-profiles.md`](docs/pr24-long-form-output-profiles.md) — long-form profiles, chapters, shared voiced scenes, and render reuse
- [`docs/pr25-project-series-channel-profiles.md`](docs/pr25-project-series-channel-profiles.md) — reusable production profiles
- [`docs/pr26-production-library-search-tagging.md`](docs/pr26-production-library-search-tagging.md) — production-library search/tagging/reuse history
- [`docs/pr27-publishing-provider-boundary.md`](docs/pr27-publishing-provider-boundary.md) — publishing authority, ledger, and crash-safety boundary
- [`docs/pr28-youtube-publishing-adapter.md`](docs/pr28-youtube-publishing-adapter.md) — YouTube upload/scheduling adapter and local OAuth boundary
- [`docs/pr29-versioned-publication-declarations.md`](docs/pr29-versioned-publication-declarations.md) — versioned exact publication declarations
- [`docs/pr32-phone-create-video-presets.md`](docs/pr32-phone-create-video-presets.md) — phone Create video presets, deterministic create identity, and exact ordered source evidence
- [`docs/pr33-project-specific-phone-flow.md`](docs/pr33-project-specific-phone-flow.md) — project-specific bounded editing, preview approval/rejection, final lifecycle, and phone projection authority
- [`SECURITY.md`](SECURITY.md) — private vulnerability reporting and supported security scope
- [`THIRD_PARTY.md`](THIRD_PARTY.md) — third-party software, runtime tools, and media licensing boundary

## Repository hygiene

The public repository contains code, schemas, documentation, tests, synthetic fixtures, and redistributable example assets only. Production libraries, downloaded media, artist works, game/anime footage, cookies, credentials, generated voice data, databases, previews, OCR model weights, and exports are local runtime data and must not be committed.

## License

Content Forge source code and repository-owned documentation/assets are licensed under the [Apache License 2.0](LICENSE) unless a file states otherwise.

Third-party dependencies, external runtime tools such as FFmpeg, and user/production media retain their own licenses and rights. See [`THIRD_PARTY.md`](THIRD_PARTY.md) for the current boundary.

For security vulnerabilities, follow [`SECURITY.md`](SECURITY.md) rather than opening a public issue.
