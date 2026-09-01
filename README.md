# content-forge

Local-first modular content production system for short-form and long-form media.

`content-forge` is designed around a simple workflow: collect source material quickly from a phone, turn it into structured projects, review only the decisions that need human attention, and let the desktop worker handle rendering, audio, previews, QC, and export.

The project starts with YouTube Shorts-style workflows, but the core model is intentionally platform- and format-agnostic. The same timeline should be able to produce short vertical edits, art stories, panel sequences, voiced comic/manhwa scenes, and later long-form video.

## Core principles

- **Local-first.** Source libraries, generated media, credentials, and production state stay on the user's machine by default.
- **Phone-first ingest and review.** Finding material and approving small decisions should not require sitting at the desktop.
- **Content kind, presentation template, and workflow are separate concepts.** A character moment can use a clean hook, a meme layout, a frame, or a synchronized stack without changing the source model.
- **One scene/timeline runtime.** Video, images, image sequences, text, reactions, subtitles, TTS, music, and transitions compile into the same render plan.
- **LLMs are optional providers, not infrastructure.** `chatgpt-web-adapter` can help with hooks, OCR cleanup, translation, metadata, and classification without making rendering depend on an LLM.
- **Human attention is explicit.** Automation proceeds until a real judgment is needed, then creates a small review task instead of silently guessing.
- **Reproducible output.** Projects retain source hashes, accepted text, exact template/component versions and definition evidence, provider parameters, and render metadata.
- **Extensible by composition.** New content formats should usually require a template, component, provider, or workflow plugin—not changes to the core renderer.

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
```

## Development status

PR1–PR22 are complete in the intended post-merge repository state. The repository includes canonical domain contracts, content-addressed local storage and provenance, deterministic timeline compilation, the generic FFmpeg backend, durable preview/final render orchestration, authenticated local FastAPI and Inbox ingest, the installable phone-first PWA/share flow, the review/proxy-preview/final-production lifecycle, the exact-version template/component/skin registry boundary, the first registered non-voiced template pack, reusable semantic overlay/motion components, deterministic music/audio composition with lossless premaster caching, two-pass loudness mastering and audio QC, the optional LLM provider boundary backed by `chatgpt-web-adapter` with proposal-only review authority, language variants with localized metadata/font intent plus deterministic variant-specific preview/final cache identity, the v0.1 batch-production boundary with crash-safe frozen-plan recovery and reproducibility evidence, retained panel OCR/correction authority, explicit dialogue reading order and speaker assignment with accepted-state provenance revalidation, a durable per-line TTS layer with replaceable provider contracts, pinned Qwen3-TTS integration, content-addressed generated audio, exact cache identity, independently verified WAV evidence, a persistent Voice Cast registry that keeps narrative character identity separate from reusable voice identity while pinning immutable cast revisions and reference-audio evidence, and a derived voiced-story layer that materializes verified dialogue WAVs plus deterministic phrase timing into the shared Scene/timeline runtime with reversible ownership and fail-closed render validation.

The current implementation step is **PR23 — Voiced scene audio mix and camera choreography** in **Milestone 5 — Voiced panels and persistent cast**. PR23 builds presentation-level dialogue sequencing, ambience/music ducking, pause policy, speaker-aware camera motion, reusable scene presets, and voiced-scene QC over the PR22 materialized scene graph without creating a second mixer or timeline runtime.

The intended v0.1 vertical slice is implemented through PR17:

```text
Phone upload/share
  -> Inbox
  -> Project
  -> registered template
  -> fast preview
  -> approval
  -> 1080x1920 render
  -> batch/QC
  -> reproducibility + export
```

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

See [`docs/pr8-local-api.md`](docs/pr8-local-api.md) for the PR8 API/Inbox contract, [`docs/pr9-pwa.md`](docs/pr9-pwa.md) for the PWA/share flow, [`docs/pr10-review-preview.md`](docs/pr10-review-preview.md) for the review/preview production contract, [`docs/pr11-template-registry.md`](docs/pr11-template-registry.md) for the versioned template/component extension boundary, [`docs/pr12-initial-template-pack.md`](docs/pr12-initial-template-pack.md) for the initial registered format pack, [`docs/pr13-reusable-components.md`](docs/pr13-reusable-components.md) for reusable overlays/motion, [`docs/pr14-music-audio.md`](docs/pr14-music-audio.md) for deterministic audio composition/mastering, [`docs/pr15-llm-provider.md`](docs/pr15-llm-provider.md) for the optional LLM/provider proposal boundary, [`docs/pr16-language-variants.md`](docs/pr16-language-variants.md) for language variants/localized cache identity, [`docs/pr17-batch-qc.md`](docs/pr17-batch-qc.md) for durable batch/QC/reproducibility, [`docs/pr18-ocr-panel-text.md`](docs/pr18-ocr-panel-text.md) for retained local OCR/correction authority, [`docs/pr19-dialogue-scene-speaker-assignment.md`](docs/pr19-dialogue-scene-speaker-assignment.md) for dialogue reading-order/speaker authority and provenance, [`docs/pr20-tts-qwen.md`](docs/pr20-tts-qwen.md) for the durable TTS/Qwen integration and cache/evidence boundary, [`docs/pr21-voice-cast.md`](docs/pr21-voice-cast.md) for persistent cast revisions, project bindings, reference-audio identity, and guarded PR21→PR20 synthesis, and [`docs/pr22-voiced-story-review-timed-text.md`](docs/pr22-voiced-story-review-timed-text.md) for derived voiced-story timing, reversible scene materialization, listen/regenerate review, and fail-closed render authority.

## Initial content families

The current non-voiced pack covers:

- clean top-hook and top-bar layouts;
- social-post and meme-header presentation;
- generic framed content for anime/game/clip use cases;
- single-art and multi-art stories with bounded source-credit handling;
- comic, manga, and manhwa panel sequences;
- synchronized two/three-copy layouts;
- bottom reaction compositions with explicit reaction-asset provenance;
- reusable artist credits, comments, reactions, avatars, watermarks, pan/zoom/reveal motion, and simple transitions;
- deterministic original/music mixing with fades, timeline ducking, two-pass loudness mastering, peak protection, QC, and lossless premaster caching.

Optional language/semantic assistance is available through the PR15 provider boundary, PR16 can compile multiple localized variants from one shared source/timeline graph, and PR17 can prepare/render/QC those outputs as authenticated durable batches. Milestone 5 now has retained panel OCR, explicit dialogue reading order/speaker identity, durable per-line TTS with pinned local Qwen support, persistent reusable Voice Cast identity, and materialized voiced-story timing/text/audio over the shared Scene runtime; presentation-level voiced-scene mixing and camera choreography remain PR23 work rather than a second timeline runtime.

See [`docs/content-formats.md`](docs/content-formats.md) for the current taxonomy.

## Documentation

- [`ROADMAP.md`](ROADMAP.md) — staged implementation plan
- [`docs/vision.md`](docs/vision.md) — product goals and boundaries
- [`docs/architecture.md`](docs/architecture.md) — domain and runtime architecture
- [`docs/content-formats.md`](docs/content-formats.md) — content kinds, templates, and composition model
- [`docs/workflows.md`](docs/workflows.md) — project lifecycle and review model
- [`docs/phone-first-workflow.md`](docs/phone-first-workflow.md) — mobile ingest/review design
- [`docs/rendering-model.md`](docs/rendering-model.md) — timeline, render plan, FFmpeg/Pillow responsibilities
- [`docs/providers.md`](docs/providers.md) — LLM, OCR, TTS, source, and future provider interfaces
- [`docs/safety-and-provenance.md`](docs/safety-and-provenance.md) — source tracking, credits, permissions, platform risk
- [`docs/v0.1-spec.md`](docs/v0.1-spec.md) — first implementation contract
- [`docs/pr8-local-api.md`](docs/pr8-local-api.md) — authenticated local API and Inbox contract
- [`docs/pr9-pwa.md`](docs/pr9-pwa.md) — PWA, Android share target, pairing, and phone Inbox flow
- [`docs/pr10-review-preview.md`](docs/pr10-review-preview.md) — review queue, proxy preview, approval, and production-decision contract
- [`docs/pr11-template-registry.md`](docs/pr11-template-registry.md) — exact-version template/component/skin registry and provenance contract
- [`docs/pr12-initial-template-pack.md`](docs/pr12-initial-template-pack.md) — registered initial non-voiced template pack and resolver boundaries
- [`docs/pr13-reusable-components.md`](docs/pr13-reusable-components.md) — reusable semantic overlay, motion, transition, and text-overflow contracts
- [`docs/pr14-music-audio.md`](docs/pr14-music-audio.md) — music/original mixing, lossless premaster, two-pass loudness, QC, and cache contracts
- [`docs/pr15-llm-provider.md`](docs/pr15-llm-provider.md) — optional task-oriented LLM provider, strict output validation, proposal authority, and `chatgpt-web-adapter` integration
- [`docs/pr16-language-variants.md`](docs/pr16-language-variants.md) — localized variant snapshots, shared timeline compilation, portable font intent, and variant-specific cache identities
- [`docs/pr17-batch-qc.md`](docs/pr17-batch-qc.md) — durable batch preparation, crash recovery, QC, reproducibility, and export-sidecar integrity
- [`docs/pr18-ocr-panel-text.md`](docs/pr18-ocr-panel-text.md) — replaceable local OCR, retained raw/corrected panel text, geometry/confidence evidence, and bounded review authority
- [`docs/pr19-dialogue-scene-speaker-assignment.md`](docs/pr19-dialogue-scene-speaker-assignment.md) — dialogue scene, reading-order, speaker-assignment, focus-hint, review-authority, and accepted-state integrity contract
- [`docs/pr20-tts-qwen.md`](docs/pr20-tts-qwen.md) — replaceable TTS provider, immutable Qwen snapshot, semantic/cache identity, generated-audio integrity, and per-line synthesis contract
- [`docs/pr21-voice-cast.md`](docs/pr21-voice-cast.md) — persistent Voice Cast identity, immutable revisions, exact reference-audio SHA pinning, project bindings, preview, and stale-TTS invalidation contract
- [`docs/pr22-voiced-story-review-timed-text.md`](docs/pr22-voiced-story-review-timed-text.md) — derived voiced-story timing, PR22-owned overlays/audio, reversible materialization, listen/regenerate, and render-guard contract
- [`SECURITY.md`](SECURITY.md) — private vulnerability reporting and supported security scope
- [`THIRD_PARTY.md`](THIRD_PARTY.md) — third-party software, runtime tools, and media licensing boundary

## Repository hygiene

The public repository contains code, schemas, documentation, tests, synthetic fixtures, and redistributable example assets only. Production libraries, downloaded media, artist works, game/anime footage, cookies, credentials, generated voice data, databases, previews, OCR model weights, previews, and exports are local runtime data and must not be committed.

## License

Content Forge source code and repository-owned documentation/assets are licensed under the [Apache License 2.0](LICENSE) unless a file states otherwise.

Third-party dependencies, external runtime tools such as FFmpeg, and user/production media retain their own licenses and rights. See [`THIRD_PARTY.md`](THIRD_PARTY.md) for the current boundary.

For security vulnerabilities, follow [`SECURITY.md`](SECURITY.md) rather than opening a public issue.
