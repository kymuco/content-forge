# Deterministic timeline compiler

PR4 introduces the boundary between canonical project/template semantics and future render backends.

The compiler consumes:

```text
Project
+ selected Variant
+ selected OutputProfile
+ ResolvedTemplate
+ Asset resolver
```

and produces one renderer-independent `RenderPlan`.

The plan contains no FFmpeg commands, Pillow operations, provider calls, content-kind branches, database paths, or UI state.

## Why this boundary exists

A render backend should not need to understand concepts such as `character_moment`, `manga_panels`, `hook_overlay`, or a future plugin format. Those concepts are resolved upstream into ordinary scenes, overlays, audio tracks, transitions, and assets.

This keeps the rendering core reusable:

```text
content/workflow/template logic
            ↓
     ResolvedTemplate
            ↓
Project → TimelineCompiler
            ↓
        RenderPlan
            ↓
      FFmpeg/Pillow
```

## Geometry

PR4 follows the frozen PR2 domain contract: canonical and planned geometry remains normalized in `[0, 1]`.

Pixel dimensions exist only in `OutputProfile`.

For example, the same planned rectangle can be resolved later against both:

```text
preview_vertical  540 × 960
short_vertical   1080 × 1920
```

This is intentional and supersedes the early PR1 wording in `rendering-model.md` that described final-canvas pixel geometry inside the normalized plan. PR5 is responsible for the deterministic normalized-to-pixel conversion.

## Scene scheduling

Scenes have explicit `order` values and PR4 requires them to be contiguous from zero. Scene IDs must also remain unique after template resolution.

Without transitions:

```text
scene 0: 0.0 ───── 4.0
scene 1:             4.0 ───── 7.0
```

A transition creates intentional temporal overlap:

```text
crossfade = 0.5 s

scene 0: 0.0 ───────── 4.0
scene 1:           3.5 ───────── 6.5
                  ←0.5→
```

The transition may be declared as `transition_out` on the preceding scene, `transition_in` on the following scene, or both. If both are present they must be identical.

Compiler invariants:

- first `transition_in` and last `transition_out` are cuts;
- a `cut` has zero duration;
- a non-cut transition has positive duration;
- a transition cannot exceed either adjacent scene duration;
- incoming plus outgoing overlap cannot exceed an intermediate scene's duration.

These rules prevent accidental triple-overlap or contradictory timing semantics from leaking into the backend.

## Overlay timing

Scene-local overlay times are relative to their scene before compilation.

```text
scene starts at 3.5
local overlay starts at 0.25
```

becomes:

```text
absolute overlay start = 3.75
```

If an overlay has no explicit duration, it lasts until the end of its scope. Project/template overlays are scoped to the complete timeline.

All visual overlays must have a resolved normalized placement before the plan is accepted. Slot/anchor resolution belongs upstream in template/component logic. A visual overlay cannot reference an audio-only asset.

## Variant text resolution

`Overlay.variant_field` is resolved before rendering.

Precedence:

1. `Variant.text_overrides[field]`;
2. built-in variant fields (`hook`, `title`, `description`);
3. the overlay's literal fallback text.

If a requested field cannot be resolved and no fallback exists, compilation fails.

This means the render backend receives concrete text rather than language/business logic.

## Audio timing

Audio timing follows the same scope model as overlays.

A scene-local track is converted to absolute timeline seconds. `track_type="original"` may omit `asset_ref`; the compiler then binds it to the containing scene media and carries the scene source trim into `source_start_seconds`.

Known source durations are checked before rendering. Looping tracks may intentionally outlive their source asset; non-looping tracks may not. Image assets and assets explicitly known to contain no audio cannot be used as audio tracks.

## Asset resolution

Every referenced media asset is resolved through either:

- a mapping of `asset_id -> Asset`; or
- an object implementing `get_asset(asset_id)` such as the PR3 library database.

Unknown assets fail compilation. A resolver must also return an `Asset` whose own `asset_id` exactly matches the requested identity; a mapping entry cannot silently substitute another asset.

The final plan contains a sorted asset table with stable identity/hash/media metadata so later render manifests can include reproducibility evidence without reinterpreting project semantics.

## Template boundary

`ResolvedTemplate` is the normalized output of the future template/plugin layer:

```text
template_id
version
scenes          # optional fully resolved replacement scene graph
overlays
audio_tracks
properties
```

It is not itself the template registry.

`scenes=None` preserves `Project.scenes`. When `scenes` is supplied, it fully replaces the project scene graph for this compilation. This is the content-agnostic mechanism future templates can use to change placement, repeat or stack media, or otherwise resolve presentation into ordinary scenes without teaching the timeline compiler or renderer about a named content format.

All replacement scenes still pass through the same deterministic order, transition, source-bound, asset-identity, overlay, and audio validation as project scenes.

The compiler requires `ResolvedTemplate.template_id/version` to match `Project.template` exactly. A project with no template reference must not receive a resolved template contribution.

PR6 can therefore implement `hook_overlay` as ordinary upstream resolution logic without adding `if template == ...` branches to the renderer.

## Render-plan invariants

`RenderPlan` and its timed child models validate their own redundant timing/reference structure rather than trusting only the compiler that constructed them.

In particular:

- each planned `end_seconds` must equal `start_seconds + duration_seconds`;
- total plan duration must equal the final scene end;
- scene/overlay/audio/asset identities are unique in their respective plan namespaces;
- scene scopes referenced by overlays/audio must exist;
- every referenced media asset must appear in the plan asset table;
- planned overlays/audio cannot extend beyond total plan duration.

This makes serialized or externally reconstructed plans fail closed before PR5 consumes them.

## Determinism

The compiler:

- sorts scenes by explicit order;
- sorts resolved overlays by absolute start, z-index, and stable ID;
- sorts audio by absolute start and stable ID;
- sorts referenced assets by stable asset ID;
- canonicalizes timeline arithmetic to nine decimal places;
- uses strict JSON for plan hashing.

`render_plan_digest(plan)` returns a SHA-256 digest of the semantic plan.

The digest changes when meaningful render semantics change and remains stable across repeated compilation of the same inputs.

## Error boundary

PR4 exposes specific compile errors:

```text
TimelineCompileError
├── MissingTimelineAssetError
├── TimelineBoundsError
├── TimelineTransitionError
├── TimelineSelectionError
└── TemplateResolutionError
```

These are pre-render failures. PR5 should not try to repair them implicitly.

## Current deliberate limitations

PR4 does not implement:

- template discovery/registry;
- FFmpeg or ffprobe;
- normalized-to-pixel rounding;
- text layout/wrapping;
- advanced motion-specific semantic validation;
- thumbnail/proxy generation;
- rendering or QC.

Those remain later roadmap layers.

## Tests

The test suite contains invariant tests and a semantic render-plan snapshot. The snapshot freezes scene timing, transition overlap, variant text, global/template layers, audio source offsets, referenced assets, and output-profile identity using synthetic data only.

Additional hardening coverage exercises template scene replacement, resolver identity mismatch, missing plan asset-table entries, invalid planned timing identities, source trims when probe duration is unknown, and media-type misuse across visual/audio layers.
