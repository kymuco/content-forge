# PR16 — Language variants and localized render metadata

PR16 closes the language-variant boundary without introducing a second project model,
a second timeline, or per-language copies of source media.

The canonical shape remains:

```text
Project
├── source records / assets
├── scenes / timing / transitions
├── output profiles
└── variants
    ├── EN
    ├── JA
    └── KO
```

A `Variant` contains language/presentation data only. Scenes, asset references, source
provenance, timing, transitions, and output profiles remain on the containing `Project`.
The existing timeline compiler selects one `variant_id` and resolves `variant_field`
text against that variant while compiling the same shared scene graph.

PR16 adds `compile_language_variant(...)` as the language-aware entrypoint. It freezes
the exact localized metadata snapshot and compiles the matching render plan in one
operation, returning `CompiledLanguageVariant`. This avoids pairing a stale render plan
with newer accepted localized text when constructing cache identities.

## Localized metadata snapshot

`content_forge.variants.LocalizedVariantSnapshot` is the bounded PR16 metadata view of
one accepted canonical `Variant`.

It contains:

- `variant_id`;
- language tag;
- hook;
- title;
- description;
- hashtags;
- text overrides, including the reserved `subtitle` key;
- style overrides, including the reserved `font` key.

It deliberately contains no scenes, assets, source records, or output profiles. This
keeps localization metadata independent from shared project media/timeline state.

`localized_variant_digest(...)` hashes this normalized snapshot deterministically.
That digest changes when accepted localized metadata changes, including metadata such as
title or hashtags that may not alter video pixels in a particular template.

## Subtitle contract

PR16 does not add a second subtitle field to the core schema. Subtitle text is stored in
the already-versioned canonical variant map:

```python
variant.text_overrides["subtitle"]
```

A text overlay with `variant_field="subtitle"` therefore resolves through the same
existing timeline rule as any other named text override.

`build_language_variant(...)` provides a convenience `subtitle=` argument and maps it to
that reserved key. Supplying conflicting values through both `subtitle=` and
`text_overrides["subtitle"]` fails closed.

## Font selection

Optional language-specific font selection is represented as a portable semantic token:

```python
variant.style_overrides["font"] = "NotoSansJP"
```

PR16 intentionally does **not** store filesystem font paths in canonical variants.
Paths such as `C:/Fonts/...` or `/usr/share/fonts/...` are machine/runtime details and
would make accepted metadata non-portable.

The PR16 font token is an ASCII family/alias token compatible with the existing FFmpeg
`drawtext` font option contract. `compile_language_variant(...)` decorates ephemeral
project/template copies so that the selected font is applied only to overlays with an
explicit `variant_field`. Nonlocalized credits and other unbound text remain unchanged,
and the stored canonical project is not mutated.

`apply_localized_text_style(...)` exposes the same small resolver rule for reusable
component/template code. Other arbitrary `style_overrides` remain metadata until a
future version gives them renderer-independent semantics.

## Atomic plan + metadata pair

`CompiledLanguageVariant` contains:

- the resolved `RenderPlan`;
- the exact `LocalizedVariantSnapshot` used to compile it.

Its constructor rejects variant-ID or language disagreement. The public cache helpers
consume this pair rather than accepting an unrelated current `Variant`, so a caller
cannot accidentally compute a fresh cache key for an old plan after accepted rendered
text changes. Metadata edits therefore require a fresh language compile before a new
cache identity is produced.

The compilation copies are ephemeral. Scene IDs, media/source references, duration,
transitions, and output profile identity remain shared with the master project.

## Variant-specific render cache identity

`variant_render_cache_identity(...)` and `variant_render_cache_key(...)` create stable
preview/final cache identities from one `CompiledLanguageVariant`:

- cache-key contract version;
- preview/final purpose;
- project ID;
- variant ID and language;
- output profile ID;
- exact template ID/version when present;
- deterministic render-plan digest;
- deterministic localized-variant digest.

This means:

1. EN, JA, and KO cannot collide even when they share every source asset and scene;
2. preview and final cache entries cannot collide;
3. changing accepted localized metadata invalidates the variant cache identity after a
   fresh language compile;
4. stale plan/current-metadata pairing is not part of the cache API;
5. when an output profile declares `properties["purpose"]`, the requested cache purpose
   must match it.

The cache key is semantic and content-based. It does not depend on temporary filesystem
paths or provider session state.

## Why RenderPlan 1.0 is unchanged

`RenderPlan` already carries `variant_id` and `variant_language`, and its resolved overlay
text already changes when a rendered language field changes. PR16 therefore does not add
new fields to the persisted `RenderPlan 1.0` schema merely to repeat metadata.

That avoids silently changing historical render-plan digests and preserves the PR7
render-attempt integrity contract. Localized metadata that is not part of the pixel/audio
timeline is snapshotted and hashed separately by PR16.

## Synthetic EN / JA / KO evidence

`tests/test_pr16_language_variants.py` builds one synthetic project with:

- one shared image asset;
- one shared scene and timing graph;
- one hook overlay;
- one subtitle overlay;
- EN, JA, and KO language variants.

The tests verify that all three compiled plans preserve the same scene ID, media asset
ID, asset table, and duration while resolving different language text and variant font
intent. They also verify that canonical project overlays remain unchanged, localized
metadata is bounded/normalized, cache keys are deterministic and purpose-specific, an
invalid compiled plan/snapshot pair fails closed, and a metadata-only edit changes the
localized cache identity after recompilation.

## Boundaries left for PR17 and later

PR16 defines language-variant semantics and cache identity. It does not add a new batch
queue, export sidecar format, translation provider, font downloader, or font discovery
service.

PR17 can persist/use these variant cache identities while adding batch preparation,
render queues, QC, reproducibility manifests, and export sidecar metadata.

## Exit condition

PR16 is complete when one canonical source project can produce multiple language
variants without duplicating media or timelines, with localized hook/subtitle/title/
description/hashtags/font intent and deterministic variant-specific preview/final cache
identity.
