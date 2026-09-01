# PR24 — Long-form output profiles

PR24 extends the existing Content Forge Scene/timeline/render architecture to horizontal long-form output. It does not introduce a second renderer, timeline, mixer, subtitle authority, voiced-scene authority, or cache database.

## Authority boundary

The canonical `Project.scenes` order remains the only edit/timing authority. The existing timeline compiler concatenates scenes deterministically, validates contiguous order/transitions, and produces the renderer-independent `RenderPlan` consumed by the existing FFmpeg backend.

PR24 adds four things over that authority:

1. canonical horizontal 16:9 `OutputProfile`s;
2. chapter metadata derived from the already-compiled Scene order;
3. exact cross-project references/materialization for reusable voiced scenes;
4. authenticated reuse lookup over already-succeeded PR7 render attempts.

PR19 character identity, PR20 generated-audio evidence, PR21 cast identity, PR22 timing/audio materialization, and PR23 presentation remain owned by their existing layers.

## Built-in long-form profiles

PR24 introduces:

- `long_form_1080p`: 1920x1080, 30 fps default, H.264/AAC, 12 Mbps video / 192 kbps audio;
- `long_form_1440p`: 2560x1440, 30 fps default, H.264/AAC, 24 Mbps video / 192 kbps audio.

Both use the existing `OutputProfile` model and declare `purpose=final`, `orientation=horizontal`, `format_family=long_form`, and `aspect_ratio=16:9`.

Project/Scene geometry stays normalized. The same canonical Scene graph therefore compiles to either profile without a second horizontal edit graph. PR23 normalized camera intent is likewise resolved against the selected output dimensions by the existing renderer.

## Chapter composition

A chapter is metadata, not a timeline.

`compile_long_form_composition()` first delegates to `compile_timeline()` and derives chapter intervals from the resulting `PlannedScene` schedule. Flattening every chapter's `scene_ids` must exactly equal the canonical compiled Scene order, so chapter metadata cannot omit, duplicate, reorder, overlap, or retime scenes.

Versioned contracts:

- `pr24_long_form_chapter_spec_v1`;
- `pr24_long_form_chapter_plan_v1`;
- `pr24_long_form_composition_v1`.

The composition pins project ID, selected profile ID, semantic `render_plan_digest`, total duration, and deterministic chapter start/end/duration. With no explicit chapter specification, one chapter covers the whole canonical timeline.

## Shared voiced scenes

PR24 does not treat a copied PR22/PR23 Scene as new local voiced authority. Reuse starts with `SharedVoicedSceneRef`, captured only from a currently valid PR23 source Project.

The reference pins exactly the source evidence relevant to the reusable Scene:

- `source_project_id`;
- `source_scene_id`;
- exact materialized source `Scene` SHA-256;
- SHA-256 of every `SourceRecord` referenced by that Scene;
- exact PR22 scene SHA-256 carried by the current PR23 plan;
- exact PR23 scene-plan SHA-256.

This intentionally does not pin the entire source Project manifest: unrelated preview/job bookkeeping must not invalidate an otherwise unchanged reusable Scene. Conversely, edits to the Scene, its relevant source provenance, PR22 timing/audio authority, or PR23 scene presentation fail closed.

### Host materialization

`LongFormSharedSceneWorkflow` creates a deterministic host-owned snapshot rather than importing source authority markers directly.

For each shared Scene it:

- assigns deterministic new Scene/Overlay/AudioTrack IDs scoped to the host Project and source reference;
- preserves scene-local rendered semantics such as voice placement, timed text, camera motion, and scene-local ducked audio;
- removes PR22/PR23 owner markers so the host never pretends to own source PR22/PR23 authority;
- records PR24 source Project/Scene/Scene-SHA/provenance-SHA/PR22/PR23 evidence on the copied Scene;
- copies only required `SourceRecord`/source-reference provenance into the host;
- records exactly which provenance objects PR24 itself added;
- rejects transitive PR24 sharing, deterministic ID collisions, source PR24 namespace collisions, duplicate references, self-project references, and neighbor-dependent non-cut transitions.

A shared Scene is appended after the host's existing contiguous Scene sequence. Chapters may then group the resulting canonical Scene order; they still cannot reorder it.

Global music/ambience from the source Project is deliberately not imported merely because one Scene is shared. Reuse is scene-local. A long-form Project owns its own global soundtrack/mix choices.

### Reversibility and lifecycle

PR24 stores separate shared-scene and provenance-ownership receipts. Materialization, replacement, and dematerialization use Project CAS and atomically invalidate stale PR10 preview/final render identity.

Dematerialization removes only PR24-owned Scene/provenance state. If the host independently starts referencing a SourceRecord that PR24 originally imported, that record is retained while the PR24-only source reference is removed.

`materialize(())` is rejected; explicit removal uses `dematerialize()` so an empty metadata receipt cannot masquerade as active authority.

## Render authority

`review_pr24_hardening` layers onto the existing PR10/PR17/PR23 `ReviewService` class without changing its public class identity.

Before the existing preview/final compile path runs, PR24 validates the exact host `Project` snapshot already selected by PR10. Every shared binding must still match:

- the current source Scene;
- current relevant source provenance;
- current PR22 scene evidence;
- current PR23 scene plan;
- the deterministic host-owned materialized Scene.

Any drift raises `ReviewNotReadyError` before the generic timeline compiler is invoked.

## Render/cache reuse

`render_plan_digest()` remains the only semantic render identity. It includes the selected `OutputProfile`, so the same Scene graph compiled repeatedly to 1080p has a stable digest while 1080p and 1440p have different digests.

PR24 adds `find_reusable_render_artifact()` as a read-only lookup over existing persistent render jobs. It creates no new cache table or artifact format.

A candidate must already be a `succeeded` PR7 render attempt with exact:

- project ID;
- purpose;
- profile ID;
- variant identity;
- template identity/version;
- `render_plan_digest`.

Digest equality alone is not trusted. Reuse reloads the frozen persisted `RenderPlan`, requires exact plan equality, and accepts the artifact only through the existing `RenderOrchestrator.load_artifact()` integrity path, including authenticated SQLite receipts, sidecar identity, output SHA, and ffprobe verification. A matching but tampered candidate is an integrity error rather than a silent cache miss.

## Real renderer coverage

The FFmpeg CI job renders the same two-Scene horizontal Project through the normal persistent pipeline to both:

- 1920x1080;
- 2560x1440.

The integration test verifies deterministic Scene scheduling, profile-specific render-plan identities, real libx264 outputs, authenticated artifact reload, successful reuse lookup, and fail-closed reuse after output tampering.

## Non-goals

PR24 does not:

- create a `LongFormRenderer`;
- create a second timeline or chapter edit graph;
- make chapters an ordering authority;
- duplicate PR22 voice or PR23 presentation authority;
- import a source Project's global soundtrack when sharing one Scene;
- create a second render cache database or artifact format;
- add series/channel defaults (PR25);
- implement publishing or analytics.
