# `hook_overlay` template

## Purpose

`hook_overlay` is the first concrete presentation template in Content Forge. It proves that a content format can be resolved entirely upstream of the renderer into the ordinary renderer-independent primitives established by PR4 and executed by the generic FFmpeg backend from PR5.

The template remains separate from content kind, workflow, and output profile.

```text
Project + Variant + OutputProfile + Asset metadata
                  |
                  v
          hook_overlay resolver
                  |
                  v
         ResolvedTemplate
     Scene + Overlay + AudioTrack
                  |
                  v
          timeline compiler
                  |
                  v
             RenderPlan
                  |
                  v
              FFmpeg
```

The FFmpeg backend contains no `hook_overlay` branch.

## Version

```text
template_id: hook_overlay
version: 1.0
```

A project must explicitly reference this template/version before the resolver will run.

## Built-in vertical profiles

PR6 adds profile presets as a separate module because output profile is not template identity:

```text
shorts_preview  540 x 960
shorts_final   1080 x 1920
```

Both use the same normalized protected UI regions. Preview and final therefore keep the same source crop/placement and hook region while typography scales with output width.

Protected regions are currently modeled for top system UI, right-side engagement controls, and bottom channel/title UI. `hook_overlay` fails resolution when its important hook region intersects a profile-provided protected safe zone.

## Default composition

The default composition is deliberately narrow:

- every source scene occupies the full normalized canvas;
- source fit is `cover` by default;
- explicit crop/focus/trim/transition data remains attached to the scene;
- one deterministic top text overlay spans the complete compiled timeline;
- hook text comes from the selected `Variant` (`text_overrides["hook"]` first, then `hook`);
- text is deterministically wrapped before FFmpeg;
- overflow fails closed instead of being silently truncated;
- drawtext receives scaled font size, outline, and box/background properties;
- original scene audio is added only when the source is video and probed metadata says audio is present.

For a video whose `has_audio` metadata is unknown, automatic original-audio resolution fails rather than guessing. Images never receive an original-audio track.

## Determinism

Template-generated overlay/audio IDs are derived from project/template/scene identity with a domain-separated SHA-256 construction. Re-resolving the same project produces the same generated IDs and the same semantic render-plan digest.

Preview and final use the same generated IDs and wrapped line breaks. Pixel typography values scale with profile width, so the two profiles remain presentation-equivalent without storing final-only pixel geometry in the canonical project.

## Variant style overrides

Variant-specific style overrides use a namespace:

```text
hook_overlay.<field>
```

Supported fields are the validated `HookOverlayConfig` fields, including hook region, fit mode, font sizing, outline/background colors, maximum lines, and original-audio policy. Unknown `hook_overlay.*` keys fail closed; unrelated template namespaces are ignored.

This keeps localization/presentation variants in `Variant` while preventing arbitrary renderer/filtergraph configuration from leaking through template properties.

## Failure boundaries

Resolution rejects, among other cases:

- missing or mismatched template references;
- ambiguous variant/profile selection;
- horizontal output profiles;
- hook/safe-zone overlap;
- missing source assets;
- non-image/video scene media;
- empty hook text or NUL input;
- hooks requiring more than the allowed wrapped line count;
- unknown video-audio metadata when automatic original audio is enabled;
- invalid or unknown namespaced style overrides;
- deterministic generated-ID collisions with canonical project state.

## Real render coverage

CI includes a real FFmpeg integration test that resolves a synthetic image project through `hook_overlay`, compiles the resulting `RenderPlan`, renders a 540x960 H.264 MP4, and verifies dimensions/duration with ffprobe.

## Deliberate limits

PR6 does not add Pillow raster cards, advanced typography, ASS subtitles, motion, API/PWA endpoints, QC, or export orchestration. The first template intentionally uses the simple text path allowed by the v0.1 specification.

More complex templates can later resolve to rasterized components or richer primitives without changing the content-kind model or adding template-specific code to the FFmpeg backend.
