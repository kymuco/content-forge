# Rendering model

## Goal

Rendering should be deterministic infrastructure. Content-specific semantics are resolved before the render backend is invoked.

The renderer consumes a normalized timeline/render plan containing concrete media references, timing, geometry, text/image overlays, audio operations, and output settings.

It should not contain branches such as:

```python
if project.content_kind == "anime_moment": ...
elif project.content_kind == "manga_panels": ...
```

Those differences belong to workflows/templates/components upstream.

## Compilation pipeline

```text
Project
  + Variant
  + Template
  + Resolved components
        |
        v
Normalized Timeline
        |
        v
RenderPlan
        |
   +----+----------------+
   |                     |
Static raster work    Time/media work
   |                     |
 Pillow                FFmpeg
   |                     |
   +----------+----------+
              |
            Encode
              |
             QC
              |
           Artifacts
```

## Timeline

A timeline is renderer-independent and ordered in time.

Conceptual structure:

```yaml
canvas:
  width: 1080
  height: 1920
  fps: 30

scenes:
  - id: s1
    duration: 3.0
    media: asset:abc
    fit: cover
    crop: null
    motion:
      type: slow_zoom
      from: 1.00
      to: 1.05

  - id: s2
    duration: 2.5
    media: asset:def
    fit: contain
    transition_in:
      type: crossfade
      duration: 0.15

overlays:
  - type: text
    text: "Her idle animation is way too good"
    start: 0.0
    end: 5.5
    region: hook

audio:
  - type: original
    source: asset:abc
  - type: music
    source: asset:music01
    gain_db: -12
```

Exact schema will be defined in implementation PRs; the invariant is that renderer-specific commands are not canonical project data.

## Media fit modes

Initial universal modes:

### `cover`
Fill target rectangle while preserving aspect ratio; crop excess.

### `contain`
Fit full source within target rectangle; remaining area comes from template/background.

### `stretch`
Possible but discouraged; only explicit use should distort aspect ratio.

### `blur_background`
A compound presentation mode can place a contained source over a blurred/expanded derivative. It should compile to ordinary layers rather than become a separate content kind.

## Geometry

Use a consistent coordinate system based on final canvas pixels after template resolution.

A resolved component should know its concrete rectangle before FFmpeg compilation:

```text
x
y
width
height
z-index
clip/mask (optional)
```

Templates may use relative anchors/slots, but the normalized render plan should be explicit.

## Safe zones

Output profiles/templates should expose protected regions for platform UI.

For a Shorts-style vertical profile, templates can reserve configurable regions for:

- top overlays/system UI;
- right-side engagement controls;
- bottom channel/title UI.

Important text and faces should not be placed in those regions by default.

Safe-zone violations can become QC warnings rather than hard failures where the composition is intentional.

## Static graphics: Pillow

Pillow should handle tasks that are awkward or brittle in FFmpeg `drawtext`, including:

- rich wrapped headers;
- comment cards;
- social-post/profile cards;
- artist-credit badges;
- decorative frame layers;
- complex static typography;
- precomposed masks/backgrounds.

These can be rendered to transparent PNG intermediates with cache keys based on their resolved inputs.

Advantages:

- predictable line wrapping;
- easier unit/snapshot testing;
- simpler FFmpeg filtergraphs;
- reusable static assets across preview/final renders where resolution permits.

## Timed text: ASS/libass

Timed subtitles/dialogue should prefer ASS/libass for:

- outlines/shadows;
- positioning;
- multi-style text;
- phrase-level timing;
- later speaker-specific styling.

v0.1 may use simpler text overlays for hooks; ASS becomes especially valuable for voiced panel workflows.

## FFmpeg responsibilities

FFmpeg should own:

- decode/encode;
- trim;
- scale/crop/pad;
- compositing raster layers;
- scene transitions;
- time-dependent image motion;
- audio extraction/mixing/fades;
- subtitle burn-in;
- output muxing;
- hardware encoding where available.

Python should construct commands/filtergraphs but should not reimplement video processing frame-by-frame unless a later feature genuinely requires it.

## Hardware encoding

The target desktop has an NVIDIA GPU, so the backend should probe and prefer supported NVENC encoders for final and preview output when they satisfy quality needs.

Rules:

1. probe actual FFmpeg encoder availability at runtime;
2. select a named encoder profile through configuration;
3. retain a software fallback;
4. record chosen encoder/settings in the render manifest;
5. never assume a specific GPU model in core logic.

## Preview profile

Preview optimizes latency:

```text
resolution: ~540x960 for vertical
codec: fast supported H.264 path
bitrate/quality: sufficient for crop/text review
fps: normally same semantic timing as final
```

Preview must consume the same timeline and template resolution rules as final output.

## Final vertical profile

Initial target:

```text
1080x1920
H.264
AAC when audio exists
30 or source/selected profile fps
platform-safe pixel format
```

Exact encoding defaults should be established through practical tests rather than frozen prematurely in architecture docs.

## Motion primitives

Initial reusable motion components:

### `hold`
No transform over scene duration.

### `slow_zoom`
Ken Burns-style scale interpolation around a focus point.

### `pan`
Move a crop window across a larger source.

### `focus_crop`
Resolve a crop around an explicit focus point/box.

### `crop_reveal`
Reveal a larger source area over time or between scenes.

### `blur_reveal`
Composite blurred and sharp versions using a timed mask/transition.

These are presentation primitives, not special art-only code.

## Synchronized stack

`sync_stack` should resolve one media source into N layer instances sharing the same source trim/timebase.

Conceptually:

```text
one decoded/trimmed stream
-> split N ways
-> scale each instance
-> place in slots
```

The FFmpeg compiler can optimize shared decode/split where practical, while the timeline model simply describes synchronized instances.

## Image sequences

Images become timed video scenes through FFmpeg or generated intermediate streams.

The timeline owns duration. Source images do not need to be converted to video ahead of time as canonical assets.

## Audio model

Audio should be represented independently of video layout.

Initial operations:

- original source audio on/off;
- music bed;
- gain;
- fade in/out;
- simple normalization/limiting policy;
- later dialogue and ducking.

For voiced scenes, each TTS line becomes a cached audio asset with explicit timeline placement. This makes text timing and scene duration straightforward.

## Render manifest

Each completed render should record enough information to diagnose/reproduce it:

```text
project ID/version
variant ID
normalized timeline hash
source asset hashes
template ID/version
component versions
renderer build/version
FFmpeg version
resolved command/filtergraph or stable representation
encoder/profile
provider-generated accepted assets/parameters where relevant
started/finished timestamps
QC result
output hash
```

The manifest is not necessarily user-facing, but it is essential for reliable iteration.

## Caching

Cache expensive/duplicate intermediate results by semantic key.

Examples:

```text
static card PNG:
  component type + resolved props + dimensions + font/version

proxy:
  source hash + proxy profile

preview:
  timeline hash + template/component versions + output profile + renderer version

TTS later:
  provider/model + voice + text + settings
```

Caches are disposable. Canonical project/source metadata is not.

## QC

Initial automated QC should cover what can be checked deterministically:

- output file exists and is decodable;
- expected dimensions;
- expected/allowed duration;
- audio presence according to profile/project expectations;
- missing source/overlay failures caught before render;
- text overflow detected during layout;
- safe-zone warnings;
- peak/loudness sanity where audio is present;
- unexpected zero-duration/black-only output where practical.

QC warnings should be structured so the UI can distinguish blocking failures from advisory concerns.

## Test strategy

Avoid relying on large copyrighted fixtures.

Use:

- generated color bars/gradients;
- synthetic tones;
- generated text/images;
- tiny redistributable sample clips;
- snapshot tests for normalized render plans;
- command/filtergraph unit tests;
- metadata assertions against rendered synthetic outputs.

The renderer should be testable in CI without the production source library or NVIDIA GPU. Hardware-specific tests can be optional/local.
