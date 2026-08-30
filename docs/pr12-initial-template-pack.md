# PR12 — Initial template pack

## Purpose

PR12 proves the PR11 extension boundary with a real non-voiced format pack. Every new
format is implemented as an exact-version `TemplateDefinition` plus a resolver that emits
existing `Scene`, `Overlay`, and `AudioTrack` primitives.

No PR12 format adds a content-specific branch to `Project`, `compile_timeline`, or the
FFmpeg backend.

```text
Project.template
      |
      v
TemplateRegistry
      |
 exact id/version
      |
      v
PR12 resolver
      |
 Scene / Overlay / AudioTrack
      |
      v
existing compile_timeline
      |
      v
RenderPlan + PR11 registry evidence
      |
      v
existing FFmpeg backend
```

## Pack identities

PR12 adds eight built-in templates at version `1.0`:

```text
hook_topbar
social_post
meme_white_header
content_frame
art_story
panel_sequence
sync_stack
reaction_bottom
```

`content_frame` is intentionally generic. The anime-frame use case is represented by
`content_frame` plus future skins/assets rather than introducing an anime-specific core
identity.

`hook_overlay@1.0` remains the original PR6/PR11 built-in and is not duplicated by PR12.

## Generic media overlay component

Two compositions need a visual asset layered over another source:

- duplicated copies in `sync_stack`;
- the lower reaction asset in `reaction_bottom`.

PR12 therefore adds one renderer-independent component contract:

```text
media_overlay@1.0
output_kind = overlay
accepts_asset = true
```

This is only the generic existing asset-overlay primitive. PR13 still owns semantic
components such as `Reaction`, `ArtistCredit`, `CommentCard`, `Avatar`, `Watermark`, and
motion components.

## `hook_topbar`

`hook_topbar` reserves a dedicated text region above source media.

- hook text comes from the selected `Variant`;
- text uses the same fail-closed drawtext coverage and normalized wrapping safety as
  `hook_overlay`;
- source scenes retain their canonical timing;
- video sources receive original audio when the project has no explicit original track;
- media is placed below the top bar using existing scene geometry.

No renderer-specific top-bar primitive exists.

## `social_post`

`social_post` provides a post-like identity/caption header and media below it.

Optional namespaced project metadata:

```text
social_post.display_name
social_post.handle
```

The selected variant hook is the content caption. If display-name metadata is absent, the
variant title may provide the display label; the final fallback is the neutral label
`Post`.

This is presentation metadata only. PR12 does not add social-network concepts to the
canonical `Project` schema.

## `meme_white_header`

The current renderer has no generic solid-rectangle primitive. PR12 deliberately does not
add one just for this template.

The template therefore represents the white meme header as a bounded white drawtext box
with black text, above media placed in the remaining canvas. The render-plan property

```text
white_header_mode = bounded_drawtext_box_v1
```

makes this limitation explicit. A future generic background/skin primitive may improve
the visual treatment without changing the template identity.

## `content_frame`

`content_frame` provides a generic framed-media composition:

- source media uses `contain` inside a central bounded rectangle;
- surrounding canvas remains available for branding/skin treatment;
- a selected variant hook is optional header text;
- the same template covers anime clips, game moments, framed art, and other branded media.

PR12 does not hard-code a specific anime/game title, channel, font, creator, or palette.

## `art_story`

`art_story` accepts one to 32 ordered image scenes.

- only image media is accepted;
- canonical project scene order and duration are preserved;
- media uses readable `contain` geometry;
- known `SourceRecord.credit_text` values may be shown in a small credit overlay;
- PR13 may later replace the basic text credit with a reusable semantic
  `ArtistCredit` component.

Motion is not invented by PR12. Existing canonical scene motion is left intact; new motion
presets remain PR13 scope.

## `panel_sequence`

`panel_sequence` accepts one to 64 ordered image scenes for comic, manga, and manhwa
reading flows.

- only image media is accepted;
- project scene durations are the pacing source of truth;
- media uses `contain` geometry for legibility;
- project transitions remain ordinary timeline transitions;
- no OCR, speaker inference, TTS, or voiced-story state is introduced.

The later voiced-panel milestones operate over the same canonical scenes.

## `sync_stack`

`sync_stack` accepts exactly one source scene and renders two or three synchronized copies.

Configuration:

```text
sync_stack.copies = 2 | 3
```

The first copy is an ordinary scene. Additional copies are ordinary asset overlays using
the same source asset. The resolver computes aspect-preserving rectangles from the source
metadata and selected output geometry.

Because those normalized rectangles are derived from output pixel geometry, every output
profile in one project must share the same canvas aspect ratio. Preview/final projects
with different aspect ratios fail closed rather than silently producing different stack
composition.

The source scene uses `contain`; additional copies use exact aspect-derived overlay
rectangles. Optional selected variant hook text occupies the reserved premise region.

## `reaction_bottom`

`reaction_bottom` accepts exactly one primary media scene plus one distinct reaction asset.

The reaction identity is supplied through namespaced project metadata:

```text
reaction_bottom.reaction_asset_id = cf_asset_...
```

The value must be a valid Content Forge asset ID and must resolve to image/video metadata.
Raw filesystem paths are not accepted.

The reaction is placed in an aspect-preserving lower rectangle using the existing asset
overlay primitive. Like `sync_stack`, all project output profiles must share one canvas
aspect ratio so preview/final normalized geometry cannot drift.

If the reaction asset already has one unambiguous source lineage in `Project.source_refs`,
the generated reaction overlay preserves that `source_id`. Multiple competing source
lineages fail closed instead of arbitrarily selecting provenance.

PR12 does not create a semantic `Reaction` component or reaction library. Those remain
PR13/application-library concerns.

## Text safety

PR12 text-bearing formats reuse the conservative PR6 drawtext safety rules:

- bounded normalized regions;
- deterministic normalized wrapping;
- preflight against every configured project output profile;
- conservative glyph-width and line-height budgets;
- no silent truncation;
- fail-closed behavior for unsupported CJK/emoji/specialized glyphs until a pinned
  font-backed text pipeline exists.

This keeps preview/final text semantics aligned with `hook_overlay` rather than creating a
second typography implementation.

## Provenance and versioning

All PR12 formats compile through the PR11 registry path. Their render plans therefore
carry canonical registry evidence containing:

- exact template ID/version;
- template definition SHA-256;
- exact component ID/version identities;
- component definition SHA-256 values;
- skin/packaged-asset evidence when a future template actually references those skins.

There is no version fallback.

## Real-render coverage

Unit regressions cover all eight template identities and their key fail-closed boundaries.
The dedicated FFmpeg integration job additionally renders synthetic fixtures for:

- `sync_stack` with three synchronized copies;
- `reaction_bottom` with a distinct lower reaction image.

Those tests use the existing asset-overlay path and therefore prove the composition-heavy
formats do not require a PR12 renderer branch.

## Explicit scope boundary

PR12 includes:

- the eight initial non-voiced presentation formats;
- one generic `media_overlay@1.0` component contract;
- deterministic resolver geometry and validation;
- basic art credit text;
- aspect-safe stacked/reaction composition;
- real FFmpeg regression coverage.

PR12 does not include:

- PR13 semantic overlay/motion components;
- plugin loading/execution;
- new FFmpeg primitives;
- a full-width solid-background renderer primitive;
- OCR/TTS/voiced-panel state;
- music composition;
- publishing;
- a second timeline representation.

The PR12 exit condition is satisfied when every initial non-voiced content family can be
represented by the same project/timeline/runtime and adding these formats required no
content-specific changes to the canonical project model, timeline compiler, or renderer.
