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

A PR23 plan therefore pins the exact PR22 scene representation it was derived from and becomes stale when PR22 changes.

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
- ambience can use a separate bounded duck amount;
- voice gain remains PR22-neutral in the first slice rather than rewriting voice tracks;
- observed pauses are checked against bounded presentation expectations, not silently retimed;
- camera motion is generated only from geometry that can be justified from current scene/focus evidence.

PR14's existing FFmpeg audio compiler already treats `voice` as a duck trigger for music. PR23 reuses that runtime instead of adding a second mixer. Ambience duck support may extend the same generic renderer primitive, but the renderer still consumes only normalized timeline state.

## Camera semantics

PR19 `SceneFocusHint` is semantic input; PR23 owns the concrete camera path.

The first choreography rules are:

- `explicit_crop`: normalize the accepted crop to a source window compatible with the output aspect, then apply a bounded slow zoom inside that region;
- `face`: derive a bounded source window centered around the accepted normalized face point and apply a subtle slow zoom;
- `speaker`: do **not** invent face geometry. Until a concrete speaker/face anchor exists, retain the existing camera and report a non-blocking `speaker_focus_geometry_missing` QC item;
- no focus hint: retain existing motion rather than manufacturing narrative intent.

PR23 does not infer a face from an OCR speech-bubble box. Text geometry and face geometry are different evidence.

## Reversible ownership

When PR23 takes ownership of a scene motion or a non-PR22 track property, its manifest retains the exact pre-PR23 presentation state.

Re-materialization must:

- verify that previously PR23-owned state still equals the expected materialization;
- refuse to overwrite externally drifted owned state;
- replace only PR23-owned presentation values;
- preserve PR22 voice/timed-text state byte-for-byte;
- restore retained base motion/track properties on explicit dematerialization.

## QC

The initial QC surface is derived rather than heuristic-only. It records:

- stale or missing PR22 materialization as blocking upstream failures;
- any unexpected dialogue overlap;
- pauses below/above the selected preset's bounded range;
- unresolved `speaker` focus geometry as a camera warning;
- unsupported or missing source geometry needed for concrete crop motion;
- presentation-state drift after materialization.

QC issues use stable codes so PWA/API presentation can remain independent of prose wording.

## Renderer boundary

PR23 continues the one-runtime rule:

`PR22 Project Scene`
→ PR23 presentation materialization
→ existing `compile_timeline()`
→ existing PR13 motion compiler + PR14 audio compiler
→ FFmpeg.

There is no PR23 render format, separate audio graph, or NLE timeline.

## Deferred work

The first PR23 implementation does not need to solve every cinematic case. Deferred extensions include:

- multi-keyframe camera paths inside one scene when multiple independently anchored speakers need cuts/pans;
- learned face/speaker association;
- side-chain envelopes with configurable attack/release curves beyond the existing deterministic duck primitive;
- spatial dialogue/panning;
- automated aesthetic scoring.

Those additions must preserve the same upstream authority and reversible-ownership boundaries.

## Exit condition

A current PR22 panel sequence can receive a deterministic, reversible presentation plan with bounded audio ducking, justified camera motion, stable QC evidence, and preview/final compatibility through the existing timeline/render stack, while PR19 dialogue, PR20 WAV evidence, PR21 cast identity, and PR22 timing/audio tracks remain authoritative and unchanged.