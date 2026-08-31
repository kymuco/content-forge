# PR13 — Reusable overlay and motion components

## Purpose

PR13 adds reusable semantic presentation components without introducing a second timeline or renderer. Component helpers resolve into the canonical `Overlay`, `MotionSpec`, and `TransitionSpec` models; ordinary timeline compilation remains unchanged.

PR13 deliberately does **not** mutate any PR12 `TemplateDefinition@1.0`. Exact template versions remain immutable. A future template that adopts `artist_credit@1.0`, `reaction@1.0`, or another PR13 identity must declare that component in a new exact template version so registry evidence stays meaningful.

## Registered component identities

All PR13 built-ins are exact version `1.0` definitions:

```text
artist_credit@1.0
comment_card@1.0
reaction@1.0
avatar@1.0
watermark@1.0
ken_burns@1.0
pan@1.0
crop_reveal@1.0
blur_reveal@1.0
transition@1.0
```

The registry still stores declarative component contracts. Resolver helpers remain ordinary Python composition functions and produce only canonical timeline primitives.

## Bounded text components

`artist_credit`, `comment_card`, and text `watermark` reuse the PR6 conservative text layout rules.

A text component:

1. normalizes and bounds input text;
2. performs deterministic wrapping once;
3. preflights the wrapped result against **every** output profile configured on the project;
4. fails closed if any profile exceeds the configured region;
5. emits an ordinary text `Overlay` with concrete layout evidence.

There is no silent truncation.

`comment_card` additionally requires explicit provenance: source-derived comments need a canonical Content Forge `source_id`; synthetic comments cannot claim one.

## Asset overlays

`avatar`, `reaction`, and image `watermark` preserve the caller-supplied `AssetRef` lineage and assign explicit semantic roles.

Aspect-safe placement uses source dimensions plus the selected output profile. Missing dimensions fail closed.

`reaction` accepts image/video assets. Still images naturally hold for the requested overlay duration. Video assets require known duration and must cover the requested duration. The component exposes loop intent, but PR13 deliberately rejects video looping because the current generic asset-overlay backend does not yet have a versioned loop/timebase contract.

## Motion components

The canonical scene model already carried motion; before PR13 the FFmpeg backend intentionally rejected every non-null motion type. PR13 makes the generic path real for:

```text
ken_burns -> MotionSpec.motion_type = slow_zoom
pan       -> MotionSpec.motion_type = pan
crop_reveal -> MotionSpec.motion_type = crop_reveal
blur_reveal -> MotionSpec.motion_type = blur_reveal
```

### Crop-window motion

`slow_zoom`, `pan`, and `crop_reveal` resolve aspect-correct normalized source crop rectangles and compile through FFmpeg `zoompan`.

Current PR13 boundary:

- image scenes only;
- `cover` fit semantics only;
- source width/height metadata required;
- no additional canonical `Scene.crop` on the same moving scene;
- unknown motion types remain fail-closed.

Video crop-window motion is intentionally deferred until source-time/trim behavior is characterized rather than silently producing unsynchronized frames.

### Blur reveal

`blur_reveal` reuses the ordinary fitted scene stream, splits it into blurred and sharp copies, and performs a deterministic timed blend. It does not introduce content-specific renderer logic.

## FFmpeg architecture

The stable PR5 compiler remains the implementation of ordinary scene fitting, overlays, transitions, audio, encoder selection, arguments, and manifests.

The public PR13 motion compiler is a thin wrapper:

```text
RenderPlan with motion
        |
clear motion on a validated copy
        |
base PR5 compile
        |
rewrite only scene_fit_N fragments
        |
restore original RenderPlan digest
```

No-motion plans delegate byte-for-byte through the existing compiler path.

`FFmpegBackend` and the public `content_forge.render.ffmpeg.compile_ffmpeg_command` use this wrapper. The low-level PR5 compiler module stays available as the stable implementation primitive.

## Transitions

`transition@1.0` validates the simple transition set already supported by the generic xfade backend:

- crossfade / fade / fadeblack / fadewhite;
- wipe left/right/up/down;
- slide left/right/up/down.

The component emits an ordinary `TransitionSpec`; timeline adjacency and duration bounds remain enforced by the timeline compiler.

## Tests

PR13 adds:

- registry identity/collision tests for all component definitions;
- text overflow and provenance regressions;
- asset identity, aspect, duration, and loop-policy regressions;
- canonical motion geometry tests;
- deterministic motion-aware FFmpeg command tests;
- fail-closed tests for unknown motion and video crop-window motion;
- real FFmpeg integration renders for slow zoom and blur reveal.

The dedicated FFmpeg CI job runs the real-motion integration coverage in addition to the existing renderer suite.

## Explicit exclusions

PR13 does not add:

- music composition, ducking, loudness normalization, or PR14 audio policy;
- video crop-window motion before source-time semantics are proven;
- silent video reaction looping;
- Pillow card rasterization/cache infrastructure;
- ASS subtitles;
- template-version rewrites;
- publishing or provider intelligence.

The exit condition is satisfied when reusable overlays, bounded typography, simple transitions, and generic image motion can be composed through the same canonical project/timeline/render runtime rather than one-off scripts.
