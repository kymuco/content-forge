# PR14 — Music and audio composition

## Purpose

PR14 extends the existing canonical `AudioTrack -> PlannedAudioTrack -> FFmpeg` path rather than introducing a second audio runtime.

The public render chain is now:

```text
RenderPlan
  -> PR14 audio compiler
  -> PR13 motion compiler
  -> PR5 base FFmpeg compiler
```

This keeps motion and non-motion renders on the same audio implementation.

## Canonical roles

PR14 uses existing `AudioTrack.track_type` and `AssetRef` contracts. Music is an ordinary ingested asset with `track_type="music"`; original/source audio uses `track_type="original"`. There is no second music storage system and provenance remains ordinary asset/source lineage.

Helpers:

- `music_track(...)`
- `original_audio_track(...)`
- `apply_audio_policy(...)`

`AudioMixPolicy` is explicit project/template policy. The renderer does not infer a policy from content kind.

## Mix controls

PR14 supports deterministic track-level:

- gain through the existing canonical `gain_db` field;
- fade-in and fade-out through `AudioTrack.properties`;
- music ducking during overlapping original/dialogue/narration/voice intervals;
- explicit looping through the existing `loop` field.

Ducking in v1 is timeline-driven rather than signal-reactive. This is intentional: identical canonical timing produces identical gain automation without a compressor threshold becoming hidden content-dependent policy.

`apply_audio_policy(...)` records `base_gain_db` so replacing or reapplying policy does not accumulate gain deltas.

## Loudness mastering

PR14 deliberately rejects one-pass hidden normalization.

Two-pass workflow:

```text
semantic premaster mix
  -> loudnorm analysis pass
  -> frozen LoudnessMeasurement
  -> canonical output-profile mastering evidence
  -> loudnorm linear apply pass
  -> peak limiter
```

When `audio_mastering.normalize=true`, the public FFmpeg compiler fails closed unless a frozen first-pass measurement is present.

The measurement contract retains:

- integrated loudness (`input_i`);
- true peak (`input_tp`);
- loudness range (`input_lra`);
- threshold (`input_thresh`);
- target offset.

The second pass uses those exact values with `linear=true`.

## Peak protection and QC

Mastering can add an `alimiter` ceiling. PR14 disables alimiter auto-level so the limiter does not silently normalize the signal back toward 0 dBFS; lookahead latency compensation is enabled.

`evaluate_audio_qc(...)` provides a renderer-independent baseline for:

- integrated loudness tolerance;
- true-peak ceiling;
- silence detection.

QC consumes a loudness measurement and an explicit `AudioMixPolicy`; it does not reinterpret project content.

## Cached audio intermediates

`audio_intermediate_cache_key(plan)` hashes only audio-affecting evidence:

- audio track IDs/roles/timing/source offsets;
- gains, loop state and audio properties;
- referenced audio asset SHA-256 values;
- output audio codec/bitrate;
- audio policy/mastering evidence;
- total duration.

Visual scene placement/overlay changes therefore do not invalidate a mastered-audio cache identity.

`AudioIntermediateCache` publishes content-addressed intermediates atomically and fsyncs file data before publication; POSIX directory metadata is also fsynced after replacement.

## Renderer evidence

When PR14 actually rewrites audio, the command manifest records:

- `audio_policy_backend=pr14_audio_policy_v1`;
- `audio_cache_key`;
- `audio_track_count`;
- `audio_normalized`.

Plans with audio but no PR14 fade/duck/mastering properties delegate unchanged to the PR13/PR5 manifest path.

## FFmpeg filters

PR14 may require:

- `afade` for fades;
- `volume` for timeline ducking;
- `loudnorm` for a frozen two-pass mastering apply pass;
- `alimiter` for peak protection.

The existing base path remains responsible for `atrim`, `asetpts`, `aformat`, `volume`, `amix`, input seeking/looping, AAC encoding, and final timeline duration.

## Tests

PR14 adds coverage for:

- policy materialization and stable base-gain evidence;
- music/original helpers;
- loudnorm JSON parsing and QC decisions;
- content-addressed intermediate publication;
- fade/duck/limiter filtergraph compilation;
- fail-closed normalization without measurement;
- frozen-measurement second-pass compilation;
- audio cache identity surviving visual-only edits;
- byte-for-byte delegation when PR14 features are absent;
- real FFmpeg premaster rendering, loudness analysis, second-pass mastering, AAC output, and audio/video probing.

## Explicit exclusions

PR14 does not add:

- automatic music selection;
- beat detection or beat-synchronous editing;
- content-reactive compressor heuristics;
- DSP plugins/VST hosting;
- multichannel surround mastering;
- publishing/platform-specific loudness guarantees;
- automatic background downloading of music.

The exit condition is predictable reusable audio composition without manual FFmpeg work while retaining explicit policy and reproducibility evidence.
