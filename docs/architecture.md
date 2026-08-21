# Architecture

## Architectural thesis

Content Forge should model media production as a composition problem, not as a collection of one-off editing scripts.

The central separation is:

```text
ContentKind != Template != Workflow != OutputProfile
```

- `ContentKind` describes what the source/project is about.
- `Template` describes spatial/visual presentation.
- `Workflow` describes preparation and review requirements.
- `OutputProfile` describes final dimensions/codec/platform constraints.

All four converge on a normalized `Timeline`, which is compiled into a renderer-specific `RenderPlan`.

## High-level runtime

```text
                 +-------------------+
                 |    Phone / Web    |
                 +---------+---------+
                           |
                      FastAPI/PWA
                           |
         +-----------------+-----------------+
         |                                   |
      Inbox                              Review Queue
         |                                   |
         +-----------------+-----------------+
                           |
                        Project
                           |
             +-------------+-------------+
             |             |             |
          Sources       Variants      Metadata
             +-------------+-------------+
                           |
                        Workflow
                           |
                        Template
                           |
                        Timeline
             +-------------+-------------+
             |             |             |
           Scenes        Overlays       Audio
             +-------------+-------------+
                           |
                    RenderPlan Compiler
                           |
             +-------------+-------------+
             |                           |
           Pillow                      FFmpeg
                                           |
                                      GPU/CPU encode
                                           |
                                          QC
                                           |
                                        Export
```

## Core domain objects

### `Asset`

A local immutable media object identified by content hash.

Representative fields:

```text
asset_id
sha256
media_type
mime_type
size_bytes
width
height
duration
fps
has_audio
storage_locator
created_at
```

An asset is not the same as a source record. The same bytes may be imported from multiple places, and source/provenance metadata can evolve independently of the immutable file identity.

### `SourceRecord`

Tracks where material came from and what is known about its creator/usage context.

Representative fields:

```text
source_id
asset_id
source_url
platform
creator_name
creator_handle
original_title
collected_at
credit_text
permission_status
permission_note
notes
```

Multiple source records may point to one deduplicated asset.

### `Project`

The canonical unit of production.

Representative fields:

```text
project_id
schema_version
content_kind
state
source_refs
variant_refs
workflow_id
template_id
project_metadata
created_at
updated_at
```

A project should not contain raw file paths as its primary identity mechanism. It references library assets.

### `Variant`

A presentation/language variant that reuses project sources and, where possible, timeline structure.

Typical differences:

```text
language
hook
subtitles
title
description
hashtags
font overrides
localized credits when necessary
```

### `Scene`

An ordered timed unit in the timeline.

Representative fields:

```text
scene_id
start/duration or ordered duration
media reference
fit mode
crop
motion
transition in/out
scene-local overlays
scene-local audio/dialogue
focus hints
```

Scenes may use video, image, or future compound media sources.

### `Overlay`

A timed composited element independent of the underlying scene media.

Initial overlay families:

```text
Text
Image
Avatar
ArtistCredit
CommentCard
Reaction
Watermark
Subtitle
SpeechBubble (later)
```

### `AudioTrack`

An independent audio element or policy.

Initial families:

```text
OriginalAudio
Music
SFX
VoiceLine (later)
```

### `ReviewTask`

A bounded question that blocks or improves automation.

Representative fields:

```text
review_task_id
project_id
task_type
status
priority
payload
suggestions
accepted_value
created_at
resolved_at
```

Examples:

- choose one of five hooks;
- confirm crop;
- confirm panel order;
- correct OCR;
- assign speaker;
- select/regenerate voice;
- confirm credit.

## Project state machine

The coarse project lifecycle is intentionally small:

```text
INBOX
  |
  v
DRAFT
  |
  v
PREPARED
  |
  +------> NEEDS_REVIEW -----+
  |                          |
  +--------------------------+
  |
  v
READY
  |
  v
RENDERING
  |
  v
QC
  |
  v
DONE
```

`NEEDS_REVIEW` should not encode every possible decision in the project state itself. Detailed blockers live in `ReviewTask` records.

Failures should be recorded on jobs/tasks without corrupting the canonical project manifest.

## Storage architecture

### Repository data

The Git repository contains only:

- code;
- documentation;
- schemas;
- tests;
- synthetic fixtures;
- redistributable example assets.

### Local runtime data

A default local data root can eventually resemble:

```text
.content-forge/
  db/
    content-forge.sqlite3
  assets/
    sha256/...
  thumbnails/
  proxies/
  cache/
    render/
    tts/
    ocr/
    llm/
  projects/
  exports/
  logs/
```

Physical storage should be boring and content-addressed; organizational views are virtual through metadata/tags rather than requiring manual folder trees.

## Service boundaries

### Core

Owns:

- domain models;
- validation;
- IDs/versioning;
- project lifecycle;
- review semantics;
- timeline model;
- normalized render plan.

Core must not import a concrete LLM, OCR engine, TTS model, or YouTube client.

### Asset library

Owns:

- ingest;
- hashing/deduplication;
- metadata extraction;
- thumbnails/proxies;
- source/provenance records;
- local search/tagging later.

### Workflow engine

Owns:

- preparation steps;
- automatic vs review decisions;
- dependency ordering;
- creation/resolution of review tasks;
- determining when a project is render-ready.

It should remain much simpler than a generic distributed workflow framework.

### Template/component registry

Owns:

- declarative layout definitions;
- slots/anchors;
- safe zones;
- skins;
- reusable component definitions;
- validation of template inputs.

### Render compiler

Transforms normalized timeline/template state into renderer-specific operations.

It should not decide what a hook means or whether a source is a `character_moment`.

### Render backends

Initial backends:

- Pillow for static graphics and complex text/card rasterization;
- FFmpeg for time-based composition, media transforms, audio, transitions, and encoding.

### API/UI

FastAPI exposes application operations. The PWA is a client of those operations.

The API should support desktop UI, mobile PWA, and potential future CLI without duplicating business logic.

### Providers

Providers are replaceable external/intelligent capabilities:

```text
LLMProvider
OCRProvider
TTSProvider
SourceProvider (later, where appropriate)
PublishingProvider (later)
AnalyticsProvider (later)
```

Provider failures must not corrupt project state. Suggestions and generated artifacts should be cached and attributable to provider/config/version where practical.

## Template model

A template defines presentation, not content semantics.

Example conceptual template:

```yaml
id: hook_overlay
version: 1
canvas:
  width: 1080
  height: 1920
slots:
  main:
    type: media
    rect: [0, 0, 1080, 1920]
    fit: cover
  hook:
    type: text
    anchor: top_center
    safe_top: 140
    max_width: 860
components:
  - hook
  - optional_credit
```

A future plugin can register a new template without adding a branch to a monolithic `if content_type == ...` renderer.

## Component model

Components provide reusable visual/temporal behavior.

Initial target components:

```text
Media
Text
Image
VideoGrid
PanelSequence
Avatar
ArtistCredit
CommentCard
Reaction
Subtitle
Watermark
KenBurns
Pan
CropReveal
BlurReveal
Transition
Music
```

Later:

```text
SpeechBubble
VoiceLine
SpeakerFocus
DialogueScene
```

Components should compile into generic timeline/render operations rather than directly invoking FFmpeg throughout business code.

## Preview vs final render

Preview and final output are profiles over the same project/timeline:

```text
preview_vertical: 540x960, low bitrate, speed-optimized
short_vertical:   1080x1920, final quality
long_1080p:       1920x1080, later
long_1440p:       2560x1440, later
```

A preview must never require a different edit representation. Otherwise preview/final drift becomes inevitable.

## Job execution

For one desktop machine, persistent SQLite jobs are sufficient.

Representative job types:

```text
probe_asset
generate_thumbnail
generate_proxy
prepare_project
render_preview
render_final
run_qc
synthesize_voice (later)
extract_ocr (later)
```

No Redis/Celery/Kafka is required for the initial architecture.

A worker claims jobs transactionally, records attempts/errors, and permits restart/resume after application or machine restarts.

## Extension points

The architecture explicitly supports four additive extension categories:

### Template plugin
Adds a new presentation/layout.

### Component plugin
Adds a reusable compositing or timing primitive.

### Provider plugin
Adds a replaceable external/intelligent capability.

### Workflow plugin
Adds preparation/review behavior for a content family.

A new format should usually be expressible using one or more of these without modifying `Project`, `Timeline`, or the FFmpeg backend.

## Invariants

The following should be treated as architectural tests, not aspirations:

1. A project can load and render with the LLM provider disabled.
2. Two projects can reference the same source asset without duplicating its bytes.
3. Template changes do not mutate source provenance.
4. Preview and final render consume the same normalized timeline.
5. A provider cannot silently overwrite an accepted human value.
6. A render can record exactly which asset hashes and template/component versions it used.
7. New templates do not require new project states.
8. Long-form output does not introduce a second timeline implementation.
