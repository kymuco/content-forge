# Vision

## What Content Forge is

Content Forge is a local-first production environment for turning discovered media into finished short-form and long-form content with as little repetitive manual editing as possible.

The system is not built around one YouTube template, one fandom, or one media type. It should accept video clips, single images, image sequences, comic/manga/manhwa panels, audio, and later voiced dialogue, then assemble them through reusable scene, overlay, audio, and template primitives.

The intended workflow is asymmetric:

- the **phone** is where source discovery and many quick decisions happen;
- the **desktop** is where storage, FFmpeg, TTS, previews, final rendering, and heavier processing happen;
- the **user** spends attention only where judgment materially improves the result.

## Why local-first

The source library may contain large files, copyrighted reference material, private production metadata, session credentials, generated audio, and hundreds or thousands of intermediates. Uploading all of this to a hosted service would add cost, latency, privacy concerns, and unnecessary platform dependencies.

Therefore the default architecture keeps:

- raw source media;
- the production library;
- project manifests;
- SQLite state;
- TTS cache;
- OCR cache;
- previews;
- exports;
- provider/session credentials

on the desktop machine.

Remote/mobile access should expose controlled application interfaces over the local network or a private overlay network, not publish the storage tree to the open internet.

## What problem it solves

The bottleneck in this style of content production is rarely the ability to place a video on a 1080x1920 canvas. The expensive parts are usually:

- finding good source material;
- remembering where it came from;
- selecting the useful moment;
- choosing a hook or framing;
- deciding panel order;
- identifying speakers;
- checking OCR;
- approving crops;
- listening to generated voices;
- keeping visual and audio style consistent;
- performing repetitive exports.

Content Forge turns these into a structured pipeline. Deterministic work is automated. Ambiguous work becomes a small review task.

## Product principles

### 1. Human attention is the scarce resource

A system that saves GPU time but requires ten minutes of clicking for every Short has failed. The primary optimization target is time and cognitive friction for the operator.

Every step should be classified as:

- `AUTO`: safe and deterministic enough to execute without interruption;
- `REVIEW`: machine preparation followed by a bounded human decision;
- `MANUAL`: intentionally human-controlled work when automation would be brittle or inappropriate.

### 2. Content meaning and presentation are separate

A `character_moment` is not a template. It may be rendered as a clean full-screen clip, a top-bar hook, a synchronized stack, or a reaction meme.

Likewise, an `art_story` may use a frame, slow pan, progressive reveal, reaction overlay, or voiced dialogue later.

The system therefore models separately:

- **content kind** — what the material means;
- **template** — how it is visually arranged;
- **workflow** — what preparation/review steps are required;
- **output profile** — where/how it is rendered.

### 3. One timeline runtime

Shorts, slideshows, art reveals, voiced panels, and long-form episodes must converge on one normalized timeline/render plan.

This avoids a future split into unrelated mini-editors and makes scenes reusable between output formats.

### 4. LLMs are optional accelerators

LLMs are useful for semantic work such as:

- hook suggestions;
- title/description variants;
- OCR cleanup;
- translation;
- tagging/classification;
- metadata suggestions.

They are a poor foundation for deterministic rendering, asset identity, storage, timing, or project state.

`chatgpt-web-adapter` therefore sits behind an `LLMProvider` interface. Rendering and project persistence must continue to work with no LLM configured.

### 5. Phone-first means more than responsive CSS

The mobile experience must support the actual discovery loop:

```text
Find something in browser/Reddit/gallery
-> Share to Content Forge
-> source reaches desktop over Wi-Fi
-> quick project card appears
-> approve/correct small decisions from phone
```

If a workflow repeatedly forces the user to walk to the desktop for a two-second choice, it should be treated as a UX defect.

### 6. Reproducibility beats hidden magic

An output should be reconstructable from:

- source hashes;
- source provenance records;
- project schema version;
- template/component versions;
- accepted text;
- scene timing;
- provider parameters;
- renderer version;
- output profile.

Machine suggestions are not canonical until accepted or otherwise recorded by policy.

### 7. Extensibility is additive

The common path for a new content format should be one or more of:

- add a template;
- add a reusable component;
- add a provider;
- add a workflow;

rather than changing core project semantics or FFmpeg internals.

## Initial target content

The design is informed by several observed families:

- funny clips with social-post or meme headers;
- anime moments inside branded frames;
- visually magnetic game/character moments with short hooks;
- reveal/news/design-change game clips;
- single and sequential fan-art stories;
- progressive/blur/crop reveals;
- manga/manhwa/comic panel packs with music and transitions;
- synchronized repeated video memes;
- reaction-image/video overlays;
- comment-card punchlines;
- later, voiced multi-speaker panel stories with a persistent synthetic cast.

The architecture should not encode the names of particular games, anime, artists, or meme characters. Those belong to local library metadata and project data.

## Non-goals for v0.1

v0.1 is not intended to be:

- a general-purpose nonlinear editor replacing Premiere/DaVinci;
- a public cloud SaaS;
- a broad scraping framework;
- an autonomous publisher;
- a fully automatic copyright/permission decision system;
- a fully automatic OCR/speaker-attribution/voice-acting system;
- an analytics-driven recommender before enough real production data exists.

The first release should instead make a small set of common content formats extremely fast and pleasant to produce.

## Long-term direction

If the core model holds, Content Forge can grow from a Shorts renderer into a reusable local media assembly environment:

```text
source discovery
-> library
-> structured content/project model
-> semantic assistance
-> human review
-> reusable scenes
-> short/long variants
-> rendering
-> publishing/analytics providers
```

The important bet is not that every future format is known today. The bet is that most formats can be represented as sources + scenes + components + audio + timing + presentation rules.
