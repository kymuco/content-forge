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
- **Reproducible output.** Projects retain source hashes, accepted text, template versions, provider parameters, and render metadata.
- **Extensible by composition.** New content formats should usually require a template, component, provider, or workflow plugin—not changes to the core renderer.

## Planned architecture

```text
Phone / Desktop
      |
   Web / PWA
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
          Timeline
   +--------+--------+
   |        |        |
 Scenes  Overlays  Audio
   +--------+--------+
            |
         Template
            |
      Render Compiler
       /           \
   Pillow          FFmpeg
                     |
                   NVENC
                     |
                     QC
                     |
                   Export
```

## Initial content families

The first architecture is being designed to cover:

- funny/reaction clips;
- anime moments;
- game and character moments (Genshin, ZZZ, Wuthering Waves, Marvel Rivals-style material);
- game news/reveal moments;
- single-art and multi-art stories;
- comic, manga, and manhwa panel sequences;
- synchronized multi-copy meme layouts;
- reaction overlays and comment-card formats;
- later: voiced panel stories with persistent voice casts and long-form output.

See [`docs/content-formats.md`](docs/content-formats.md) for the current taxonomy.

## Development status

The repository is at the architecture/foundation stage. The first implementation target is a narrow end-to-end vertical slice:

```text
Phone upload
  -> Inbox
  -> Project
  -> hook_overlay template
  -> fast preview
  -> approval
  -> 1080x1920 NVENC render
  -> QC
  -> export
```

The complete staged plan is in [`ROADMAP.md`](ROADMAP.md).

## Documentation

- [`docs/vision.md`](docs/vision.md) — product goals and boundaries
- [`docs/architecture.md`](docs/architecture.md) — domain and runtime architecture
- [`docs/content-formats.md`](docs/content-formats.md) — content kinds, templates, and composition model
- [`docs/workflows.md`](docs/workflows.md) — project lifecycle and review model
- [`docs/phone-first-workflow.md`](docs/phone-first-workflow.md) — mobile ingest/review design
- [`docs/rendering-model.md`](docs/rendering-model.md) — timeline, render plan, FFmpeg/Pillow responsibilities
- [`docs/providers.md`](docs/providers.md) — LLM, OCR, TTS, source, and future provider interfaces
- [`docs/safety-and-provenance.md`](docs/safety-and-provenance.md) — source tracking, credits, permissions, platform risk
- [`docs/v0.1-spec.md`](docs/v0.1-spec.md) — first implementation contract

## Repository hygiene

The public repository contains code, schemas, documentation, tests, synthetic fixtures, and redistributable example assets only. Production libraries, downloaded media, artist works, game/anime footage, cookies, credentials, generated voice data, databases, previews, and exports are local runtime data and must not be committed.

No software license has been selected yet.
