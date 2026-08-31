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

`apply_audio_policy(...)` records base gain/fade/duck evidence so replacing or reapplying policy does not accumulate gain deltas or turn an earlier policy default into a false explicit track override.

## Lossless premaster and loudness mastering

PR14 deliberately rejects one-pass hidden normalization.

Two-pass workflow:

```text
canonical semantic mix
  -> gain / fade / timeline ducking
  -> lossless 48 kHz stereo PCM F32LE premaster WAV
  -> derivation-keyed cache
  -> loudnorm analysis pass
  -> frozen LoudnessMeasurement
  -> canonical output-profile mastering evidence
  -> loudnorm linear apply pass
  -> peak limiter
  -> final AAC render
```

The base mix is already normalized to FFmpeg planar float audio. `compile_audio_intermediate_command(...)` therefore writes `pcm_f32le`, preserving those float32 samples rather than introducing an integer quantization step before first-pass measurement.

The intermediate compiler builds an internal black-carrier render plan with the same absolute audio tracks but no visual assets, overlays, motion, or visual source references. Audio premaster generation therefore does not require the original image/video files even though the final renderer still uses them.

Loudness targets and frozen measurements are deliberately excluded from the premaster.

When `audio_mastering.normalize=true`, the public FFmpeg compiler fails closed unless a frozen first-pass measurement is present.

The measurement contract retains:

- integrated loudness (`input_i`);
- true peak (`input_tp`);
- loudness range (`input_lra`);
- threshold (`input_thresh`);
- target offset.

FFmpeg reports genuinely silent input with non-finite sentinels (`-inf`/`inf`). Canonical models globally reject NaN/Inf, so PR14 maps those explicit loudnorm sentinels to `None`. That measurement remains valid QC evidence and is classified as silence, but `normalizable=false` and it cannot enter the second loudnorm pass.

For a normal finite measurement, the second pass uses the exact frozen values with `linear=true`.

## Peak protection and QC

Mastering can add an `alimiter` ceiling. PR14 disables alimiter auto-level so the limiter does not silently normalize the signal back toward 0 dBFS; lookahead latency compensation is enabled.

`evaluate_audio_qc(...)` provides a renderer-independent baseline for:

- integrated loudness tolerance;
- true-peak ceiling;
- silence detection, including real loudnorm silence sentinels.

QC consumes a loudness measurement and an explicit `AudioMixPolicy`; it does not reinterpret project content.

## Cached audio intermediates

`audio_intermediate_cache_key(plan)` is a deterministic **derivation key**, not a claim that the resulting WAV bytes themselves have that SHA-256.

The key hashes premaster-affecting evidence:

- audio track IDs/roles/timing/source offsets;
- gains, loop state and audio properties;
- referenced audio asset SHA-256 values;
- explicit audio policy identity/version;
- total duration;
- the fixed premaster format/version (`PCM F32LE`, 48 kHz, stereo).

It deliberately excludes:

- visual scene placement and overlays;
- loudness targets and first-pass measurement;
- final AAC codec/bitrate.

Those values are downstream of the cached lossless premaster and therefore must not invalidate it.

`AudioIntermediateCache` publishes derivation-keyed intermediates atomically, uses a unique temporary file for each publication attempt, fsyncs file data before replacement, and fsyncs POSIX directory metadata after replacement. Concurrent same-key publishers therefore do not share or delete each other's temporary files.

## Renderer evidence

When PR14 actually rewrites final audio, the command manifest records:

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

- policy materialization and stable base-gain/fade/duck evidence;
- music/original helpers;
- finite and silent loudnorm JSON parsing;
- silence QC plus fail-closed normalization of non-finite measurements;
- derivation-keyed intermediate publication;
- concurrent same-key cache publication;
- fade/duck/limiter filtergraph compilation;
- fail-closed normalization without measurement;
- frozen-measurement second-pass compilation;
- float-lossless premaster generation without visual asset paths;
- audio premaster cache identity surviving visual/mastering-only edits;
- byte-for-byte delegation when PR14 features are absent;
- real FFmpeg float-premaster rendering, loudness analysis, real silence classification, second-pass mastering, AAC output, and audio/video probing.

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
