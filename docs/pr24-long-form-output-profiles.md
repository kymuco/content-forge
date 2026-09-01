# PR24 — Long-form output profiles

PR24 extends the existing Content Forge Scene/timeline/render architecture to horizontal long-form output. It does not introduce a second renderer, timeline, mixer, or cache authority.

## Authority boundary

The canonical `Project.scenes` order remains the only edit/timing authority. The existing timeline compiler already concatenates scenes deterministically, validates contiguous scene order and transitions, and produces one renderer-independent `RenderPlan`.

PR24 adds long-form presentation/output structure over that existing authority:

- canonical horizontal 16:9 output profiles;
- deterministic chapter grouping over the already-compiled scene order;
- explicit composition evidence pinned to the exact `RenderPlan` digest;
- later shared-scene composition must retain exact source-project/evidence identity rather than treating copied scene values as new authority.

A chapter is metadata, not a timeline. Flattening all chapter `scene_ids` must exactly equal the canonical compiled scene order. Chapters therefore cannot omit, duplicate, reorder, or retime scenes.

## Built-in profiles

PR24 introduces two final-output profiles through the existing `OutputProfile` model:

- `long_form_1080p`: 1920x1080, 30 fps default, H.264/AAC, 12 Mbps video / 192 kbps audio;
- `long_form_1440p`: 2560x1440, 30 fps default, H.264/AAC, 24 Mbps video / 192 kbps audio.

Both profiles identify themselves as:

- `purpose = final`;
- `orientation = horizontal`;
- `format_family = long_form`;
- `aspect_ratio = 16:9`.

Frame rate remains an explicit caller-controlled profile parameter, as with the existing Shorts profiles.

## Profile-independent scenes

Project and Scene geometry stays normalized. The same canonical scene graph may therefore compile to 1080p or 1440p without rewriting scene timing or placement.

This also preserves the PR23 camera contract: `focus_zoom` stores normalized focus intent, while concrete crop geometry is resolved against the selected output profile during FFmpeg compilation. PR24 does not bake a vertical or horizontal crop into the Project merely because one output profile was selected.

## Chapter composition

`compile_long_form_composition()` first calls the existing `compile_timeline()` and then derives chapter boundaries from the resulting `PlannedScene` intervals.

Versioned contracts:

- `pr24_long_form_chapter_spec_v1`;
- `pr24_long_form_chapter_plan_v1`;
- `pr24_long_form_composition_v1`.

The composition records:

- exact project identity;
- exact selected profile identity;
- exact semantic `render_plan_digest`;
- total duration;
- deterministic chapter start/end/duration and scene membership.

If no chapter specification is supplied, PR24 derives one chapter covering the entire canonical timeline.

## Render/cache identity

PR24 deliberately reuses the PR7/PR17 render identity instead of creating a long-form cache database.

`render_plan_digest()` hashes the full semantic `RenderPlan`, including the selected `OutputProfile`. Therefore:

- the same Scene graph compiled twice to the same long-form profile has the same semantic digest;
- the same Scene graph compiled to 1080p and 1440p has different semantic digests;
- existing render-job/artifact receipts already carry `profile_id` plus `render_plan_digest`;
- existing artifact integrity checks remain applicable to long-form renders.

Later PR24 work may add a lookup/reuse helper over existing authenticated render receipts, but must not introduce a competing cache identity.

## Shared voiced scenes

Cross-project reuse is intentionally not implemented by blindly copying a PR22/PR23 materialized `Scene` into another Project. PR22 voice/timing and PR23 presentation manifests are project-bound authority and can become stale independently.

The shared-scene slice must therefore pin enough immutable source evidence to prove that every reused scene came from an exact source Project snapshot and exact PR22/PR23 authority. A source Project change must fail closed rather than silently changing a long-form composition.

Until that contract is implemented and tested, PR24's first slice supports canonical long-form output and chapter grouping inside one Project only.

## Non-goals

PR24 does not:

- create a `LongFormRenderer`;
- create a second timeline or chapter-edit graph;
- duplicate PR22 voice or PR23 presentation authority;
- add a second render cache identity;
- introduce series/channel defaults (PR25);
- implement publishing or analytics.
