# PR22 — Voiced story review UI and timed text

PR22 is the review/materialization layer that turns accepted PR18→PR21 voiced-panel state into a coherent story surface without creating a second editor or timeline runtime.

The core architectural rule is:

> PR22 may compose and review accepted dialogue, cast, synthesized audio, scene duration, and timed text, but it must not become a parallel authority for OCR, speaker identity, cast identity, or TTS provider evidence.

## Upstream authorities

PR22 consumes and revalidates existing accepted state:

- PR18 owns retained OCR regions, corrected text, geometry, and OCR evidence;
- PR19 owns accepted dialogue reading order, narrative speaker identity, and focus hints;
- PR20 owns synthesized line audio, exact settings, provider/model evidence, semantic cache identity, WAV evidence, and content-addressed generated assets;
- PR21 owns persistent reusable cast identity and project character→cast bindings.

PR22 fails closed when any retained upstream state is stale or internally inconsistent instead of copying it into a weaker parallel manifest.

## Materialized contract

PR22 stores `pr22_voiced_story` as derived evidence and materializes only the renderer-facing state that belongs to voiced-story composition:

1. `VoicedStoryLine` binds one accepted PR19 line to one current PR20 audio receipt and current PR21 cast evidence;
2. `VoicedStoryScene` retains the pre-PR22 `base_duration_seconds` and the current derived voiced duration;
3. phrase-level `TimedTextCue` records partition accepted text deterministically inside the verified line-audio interval;
4. PR22-owned scene `Overlay` records expose those cues to the existing timeline compiler and FFmpeg `drawtext` path;
5. PR22-owned scene `AudioTrack` records place current PR20 WAV assets at the same deterministic line starts;
6. ordinary `Scene.duration_seconds` is updated to the same derived duration so every existing renderer sees the voiced timing rather than a parallel timeline.

The generated overlay/audio IDs are deterministic and owner-tagged. Materialization removes or replaces only PR22-owned entries and refuses deterministic ID collisions with non-PR22 Project state.

## Timing semantics

PR22 does not pretend that proportional text timing is phoneme alignment.

The initial timing model is deterministic editorial timing:

- dialogue lines play in accepted PR19 reading order;
- each line uses its independently verified PR20 WAV duration;
- configurable bounded pauses are inserted between lines and at scene tails;
- phrase cues partition accepted line text deterministically;
- cue duration is apportioned by stable phrase weight within the exact line-audio interval;
- arithmetic is canonicalized through integer microseconds before persisted seconds are produced.

This is sufficient for readable timed text and reproducible scene timing. Optional forced alignment remains a later provider/PR and must be able to replace only the cue-timing derivation layer.

## Shared renderer path

PR22 deliberately does not introduce a second subtitle, audio, or NLE runtime.

After materialization, the existing renderer-independent path receives the voiced state directly:

`Project Scene`
→ scene-local PR22 `Overlay` + `AudioTrack`
→ existing `compile_timeline()`
→ existing FFmpeg drawtext/audio-mix compilation.

This means the same canonical scene graph is used for preview/final rendering. PR23 can later add camera choreography, ambience/music ducking, and richer dialogue mixing without migrating PR22 data out of a temporary timeline.

PR22 owns only basic deterministic placement of each current dialogue WAV. PR23 remains responsible for presentation-level mix decisions such as gain automation, ambience/music interaction, overlap policy, pan/spatial choices, and camera choreography.

## Reversible ownership

PR22 materialization is reversible.

For each voiced scene, `base_duration_seconds` records the duration that existed before PR22 took timing ownership. If an accepted voiced scene disappears on the next materialization, PR22:

- removes its timed-text overlays;
- removes its voice audio tracks;
- restores the retained base scene duration;
- removes stale PR22 metadata when accepted voiced dialogue becomes empty.

Unrelated overlays, audio tracks, scene media, and project state are retained. Manual `DELETE /api/v1/voiced-story/projects/{project_id}/materialization` performs the same ownership release explicitly.

## Review semantics

The panel-centric review surface shows each scene/panel with accepted text, speaker, current cast binding, synthesized audio duration, and phrase cues.

Actions reuse upstream authorities:

- OCR edits route through PR18 review/correction authority;
- speaker changes route through PR19 dialogue assignment authority;
- voice changes route through PR21 cast bindings;
- **Listen** serves the currently verified PR20 WAV without requiring a TTS provider;
- **Regenerate** routes through PR21 resolution and PR20 synthesis with an explicit `force=True` cache-refresh request;
- PR22 refreshes materialized timing after a successful regeneration when PR22 was already materialized.

No PR22 endpoint silently mutates OCR, speaker identity, cast identity, or provider evidence inside its own manifest.

## Safe regeneration

`force=True` does not bypass PR20 verification or create a second TTS cache.

It changes only one cache behavior: an otherwise valid semantic cache hit is not returned early. The new provider output must still pass the existing PR20 provider-evidence checks, PCM16 WAV validation, independent byte/geometry verification, content-addressed ingest, and exact Project CAS before it can replace the current line receipt.

Therefore a provider failure leaves the previous PR20 receipt and the previous PR22 materialization unchanged. A successful explicit regeneration may produce different audio bytes under the same semantic request/cache key; the accepted artifact identity remains the exact `audio_sha256` and asset ID stored in the PR20 receipt.

## Invalidations

A materialized voiced-story scene is current only while all retained evidence remains current, including:

- accepted PR19 scene-dialogue digest;
- line text and speaker identity;
- current PR20 semantic request/cache identity and exact audio SHA;
- current PR21 cast binding revision/digest;
- timing-policy version;
- PR22-owned core scene duration, timed-text overlays, and voice audio tracks.

A changed upstream authority or drift in PR22-owned materialized state makes the old PR22 manifest stale rather than allowing it to drift silently.

## HTTP / PWA surface

Authenticated PR22 routes provide:

- derived preview without mutation;
- current materialized manifest validation;
- materialization and explicit dematerialization;
- verified per-line WAV playback;
- per-line explicit regeneration.

The PWA remains panel-centric. It shows timing/cues and provides **Listen** / **Regenerate** controls, while upstream editing remains in the PR18, PR19, and PR21 surfaces.

## Deferred work

PR22 intentionally does not add:

- automatic speaker inference;
- a new OCR correction authority;
- a new cast registry;
- a second TTS cache;
- phoneme/word forced alignment;
- advanced dialogue/music/ambience mixing;
- camera choreography;
- long-form chapter assembly;
- publishing.

The implementation remains a narrow derived layer over PR18–PR21 while using the same Project and timeline runtime that later Milestone 5 work will extend.
