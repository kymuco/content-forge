# PR23 — Voiced scene audio mix and camera choreography

PR23 is the presentation layer over the canonical voiced-story state completed by PR22. It turns a verified voiced panel scene into a more polished composition without becoming a second dialogue, voice, timing, mixer, or timeline authority.

The central rule is:

> PR23 may shape how an already-current PR22 scene is presented, but it must not rewrite the identity or timing evidence owned by PR19–PR22.

## Authority chain

PR23 consumes the existing authority chain:

- PR19 owns accepted dialogue order, narrative speaker identity, and semantic scene focus hints;
- PR20 owns generated WAV identity and TTS invocation evidence;
- PR21 owns reusable voice identity and character→cast binding;
- PR22 owns canonical voiced line intervals, scene duration, timed-text overlays, and exact PR20 WAV placement;
- PR23 owns only presentation policy derived from that current PR22 state.

A PR23 plan pins the exact PR22 manifest and exact per-scene representation it was derived from and becomes stale when PR22 changes.

## What PR23 may materialize

PR23 may own only state that is outside PR22's exact voice/timed-text materialization:

1. `Scene.motion` when camera choreography is selected;
2. ducking/presentation properties on non-PR22 music or ambience tracks;
3. PR23 metadata describing the preset, derived camera intent, mix policy, QC evidence, and retained base presentation state.

PR23 must not mutate PR22-owned `voice` `AudioTrack` records, PR22 timed-text overlays, PR22 line starts/ends, PR20 receipts, or PR21 cast bindings.

This boundary is intentional: PR22 validates its own materialized voice tracks exactly. A presentation layer that edited those tracks in place would silently weaken the upstream evidence contract.

## Initial presentation policy

The first preset is deterministic and deliberately conservative:

- dialogue timing is consumed exactly as materialized by PR22;
- music is ducked while PR22 `voice` tracks overlap it;
- ambience uses its own bounded duck amount over those same authoritative voiced intervals;
- voice gain remains PR22-neutral rather than rewriting voice tracks;
- observed pauses are checked against bounded presentation expectations, not silently retimed;
- camera motion is generated only from geometry that can be justified from current scene/focus evidence.

PR23 extends the existing PR13/PR14 renderer path rather than adding a second mixer or motion runtime. Music and ambience ducking remain normalized timeline properties compiled into one FFmpeg graph.

## Camera semantics

PR19 `SceneFocusHint` is semantic input; PR23 owns the concrete camera path.

The first choreography rules are:

- `explicit_crop`: use the accepted crop center as normalized focus evidence and apply a bounded slow zoom;
- `face`: center a bounded slow zoom on the accepted normalized face point;
- `speaker`: do **not** invent face geometry. Until a concrete speaker/face anchor exists, retain the existing camera and report a non-blocking `speaker_focus_geometry_missing` QC item;
- no focus hint: retain existing motion rather than manufacturing narrative intent.

PR23 does not infer a face from an OCR speech-bubble box. Text geometry and face geometry are different evidence.

The materialized `focus_zoom` stores only normalized focus and bounded scale intent. Aspect-correct source geometry is resolved by the FFmpeg presentation compiler for the selected output profile. The same Project presentation can therefore compile for 9:16 and 16:9 without storing a vertical-only crop in canonical state.

## Reversible ownership

When PR23 takes ownership of a scene motion or a non-PR22 track property, its manifest retains the exact pre-PR23 presentation state.

Re-materialization must:

- verify that previously PR23-owned state still equals the expected materialization;
- refuse to overwrite externally drifted owned state;
- replace only PR23-owned presentation values;
- preserve PR22 voice/timed-text state exactly;
- restore retained base motion/track properties on explicit dematerialization.

An exact replay of the same plan is idempotent. An external edit to PR23-owned motion or track state is a conflict, not an invitation to overwrite it.

## QC

The initial QC surface records:

- stale or missing PR22 materialization as blocking upstream failures;
- any unexpected dialogue overlap;
- pauses below/above the selected preset's bounded range;
- unresolved `speaker` focus geometry as a camera warning;
- unsupported or missing source geometry needed for concrete camera motion;
- presentation-state drift after materialization.

QC issues use stable codes so PWA/API presentation remains independent of prose wording. A plan containing a blocking issue cannot be materialized.

## Renderer boundary

PR23 continues the one-runtime rule:

`PR22 Project Scene`
→ PR23 presentation materialization
→ existing `compile_timeline()`
→ PR23 presentation compiler wrapper
→ existing PR13 motion compiler + PR14 audio compiler
→ FFmpeg.

There is no PR23 render format, separate audio graph, or NLE timeline.

The public `FFmpegBackend` is wired to the presentation compiler. A real FFmpeg integration regression creates a source image, compiles `focus_zoom`, renders an MP4 through the public backend, and verifies the resulting dimensions/duration with ffprobe. This test is part of the required `ffmpeg-integration` CI job.

## Preview/final render authority

PR10 still owns preview/final rendering and its durable render receipts. PR23 adds only a fail-closed authority gate to that existing lifecycle.

For projects without materialized PR22 voiced-story state, PR10 compilation is unchanged.

For a Project that does contain materialized PR22 state, preview/final compilation requires a materialized PR23 manifest. The guard validates PR23 against the **exact Project object already held by PR10**:

1. parse current PR22 authority from that snapshot;
2. parse current PR23 materialization;
3. remove only verified PR23-owned presentation state to recover the retained base Project;
4. deterministically re-derive the PR23 plan from current PR19/PR22 evidence;
5. require exact equality with the stored PR23 plan before delegating to the existing PR10 compiler.

The guard intentionally does not call `manifest(project_id)` and take a second Project database snapshot. That avoids a TOCTOU gap between authority validation and the PR10 render claim/compile path.

## HTTP API

PR23 exposes the bounded authenticated surface under `/api/v1/voiced-scene`:

- `GET /projects/{project_id}/preview` — derive the current presentation plan without Project mutation;
- `GET /projects/{project_id}` — load and revalidate the materialized PR23 manifest;
- `POST /projects/{project_id}/materialize` — materialize the selected preset, failing closed on blocking QC;
- `DELETE /projects/{project_id}/materialization` — restore retained pre-PR23 presentation state.

The transport follows the established PR21/PR22 rules: secure transport boundary, bearer authentication before JSON parsing, JSON-only mutation bodies, required `Content-Length`, and a 64 KiB request-body cap.

## PWA surface

The persistent **Scene Presentation** panel is available independently of transient review tasks. It lets a paired device:

- choose a recent Project;
- derive presentation without mutation;
- inspect per-scene camera source/action and stable QC codes;
- inspect planned music/ambience duck targets;
- materialize current presentation;
- explicitly remove PR23 presentation while preserving PR22 timing and voice tracks.

The PWA service-worker shell advances from cache v11 to v12 and precaches `voiced-scene.js`. Known v8–v11 predecessors remain explicit migration inputs so an installed cache-first shell cannot hide the new PR23 panel behind stale HTML/JavaScript authority.

## Deferred work

The first PR23 implementation does not solve every cinematic case. Deferred extensions include:

- multi-keyframe camera paths inside one scene when multiple independently anchored speakers need cuts/pans;
- learned face/speaker association;
- richer side-chain envelopes with configurable attack/release curves;
- spatial dialogue/panning;
- automated aesthetic scoring.

Those additions must preserve the same upstream authority, reversible ownership, exact-snapshot render gate, and one-runtime boundaries.

## Exit condition

A current PR22 panel sequence can receive a deterministic, reversible presentation plan with bounded audio ducking, justified camera motion, stable QC evidence, authenticated API/PWA review, and preview/final compatibility through the existing timeline/render stack, while PR19 dialogue, PR20 WAV evidence, PR21 cast identity, and PR22 timing/audio tracks remain authoritative and unchanged.
