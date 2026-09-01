# PR22 — Voiced story review UI and timed text

PR22 is the review/materialization layer that turns the accepted PR18→PR21 voiced-panel state into a coherent story surface without creating a second editor or timeline runtime.

The core architectural rule is:

> PR22 may compose and review accepted dialogue, cast, synthesized audio, scene duration, and timed text, but it must not become a parallel authority for OCR, speaker identity, cast identity, or TTS provider evidence.

## Upstream authorities

PR22 consumes and revalidates existing accepted state:

- PR18 owns retained OCR regions, corrected text, geometry, and OCR evidence;
- PR19 owns accepted dialogue reading order, narrative speaker identity, and focus hints;
- PR20 owns synthesized line audio, exact settings, provider/model evidence, cache identity, WAV evidence, and content-addressed generated assets;
- PR21 owns persistent reusable cast identity and project character→cast bindings.

PR22 must fail closed when any upstream retained state is stale or internally inconsistent instead of copying it into a weaker parallel manifest.

## First implementation slice

The first PR22 vertical slice should introduce one derived, versioned voiced-story manifest over exact upstream evidence. It should be useful before any new FFmpeg choreography exists.

The slice will contain:

1. `VoicedStoryLine` records that point to one accepted PR19 line and one current PR20 synthesized line;
2. exact source/cast/audio evidence needed to prove the line still describes current project authority;
3. deterministic line timing inside each scene;
4. deterministic scene duration derived from synthesized line durations plus bounded pause policy;
5. phrase-level timed-text cues derived from accepted line text and audio duration without claiming forced alignment;
6. a panel-centric authenticated read surface suitable for the later PWA editor;
7. explicit invalidation when PR18/PR19/PR20/PR21 authority changes.

## Timing semantics

PR22 does not pretend that proportional text timing is phoneme alignment.

The initial timing model is deterministic editorial timing:

- dialogue lines play in accepted PR19 reading order;
- each line uses its verified PR20 audio duration;
- configurable bounded pauses are inserted between lines and at scene boundaries;
- phrase cues partition accepted line text deterministically;
- cue duration is apportioned by stable phrase weight within the exact line-audio interval;
- all derived timings are reproducible from retained upstream state and policy version.

This is sufficient for readable timed text and scene-duration materialization. Optional forced alignment remains a later provider/PR and must be able to replace only the timing derivation layer.

## Review semantics

The panel-centric review surface should show one scene/panel at a time with its accepted text, speaker, current cast binding, synthesized audio state, and derived timed text.

Actions must reuse upstream authorities:

- OCR edits route through PR18 review/correction authority;
- speaker changes route through PR19 dialogue assignment authority;
- voice changes route through PR21 cast bindings;
- regenerate/listen routes through PR21→PR20 synthesis;
- PR22 only refreshes/re-materializes derived story timing after those upstream mutations succeed.

No PR22 endpoint should silently mutate upstream identities inside its own manifest.

## Rendering boundary

PR22 may produce renderer-consumable timed-text data and later ASS text materialization, but it does not own PR23 audio mixing or camera choreography.

PR23 remains responsible for:

- final dialogue sequencing into the scene mix;
- ambience/music ducking;
- camera pan/zoom/focus choreography;
- voiced-scene preview/QC for overlap and presentation.

PR22 should therefore expose deterministic timing geometry that PR23 can consume directly.

## Invalidations

A derived voiced-story scene is current only while all retained evidence remains current, including:

- accepted PR19 scene-dialogue digest;
- line text and speaker identity;
- current PR20 line cache/audio identity;
- current PR21 cast binding revision/digest when a binding exists;
- timing-policy version.

A changed upstream authority must make old PR22 timing stale rather than allowing it to drift silently.

## Deferred work

PR22 intentionally does not add:

- automatic speaker inference;
- a new OCR correction authority;
- a new cast registry;
- a second TTS cache;
- phoneme/word forced alignment;
- dialogue/music/ambience mixing;
- camera choreography;
- long-form chapter assembly;
- publishing.

The implementation should remain a narrow derived layer over PR18–PR21 so Milestone 5 continues to share the same Project and timeline runtime.