# FFmpeg backend

## Purpose

PR5 implements the renderer-specific boundary beneath the renderer-independent timeline compiler from PR4.

The backend consumes a `RenderPlan` and produces a deterministic `RenderCommandManifest`. It does not inspect `content_kind`, template IDs, or workflow state.

## Capability probing

`probe_ffmpeg_runtime()` discovers `ffmpeg` and `ffprobe`, captures their version strings, parses available encoders and filters, and optionally performs a tiny real `h264_nvenc` encode probe.

Capability discovery deliberately distinguishes:

- encoder listed by the build;
- encoder actually usable on the current machine.

`select_h264_encoder()` prefers NVENC only when it passed the runtime probe. Otherwise it falls back to `libx264` when available.

This means a developer/CI machine without NVIDIA hardware uses the CPU path while the production RTX machine can use NVENC without a separate renderer implementation.

## ffprobe metadata

`probe_media()` invokes ffprobe with JSON output and extracts:

- container/format name;
- duration;
- first video width/height;
- average/rational frame rate;
- video/audio stream presence;
- first video/audio codec names.

`apply_probe_to_asset()` returns a fully revalidated copy of the PR2 `Asset` contract. It does not write to SQLite itself. PR8 can decide when automatic probing becomes part of Inbox ingest.

## Pixel geometry

PR4 plans remain normalized in `[0, 1]`. PR5 owns the first normalized-to-pixel conversion.

`resolve_pixel_rect()` rounds rectangle **edges** with deterministic half-up rounding and derives width/height from the rounded edges. Adjacent normalized slots therefore share the same resolved boundary instead of independently rounded widths drifting by one pixel.

H.264 output canvas dimensions must currently be even.

## Scene video composition

Every planned scene is compiled to a full output-sized stream beginning at PTS zero. The backend supports:

- image and video scene sources;
- `contain`, `cover`, and `stretch` fit modes;
- normalized explicit crop rectangles;
- source trim/seek;
- scene placement on the output canvas;
- static image looping for the scene duration;
- ordered scene concatenation;
- supported visual transitions through FFmpeg `xfade`.

The command compiler rejects renderer-independent constructs it cannot faithfully implement instead of silently changing their meaning.

## Overlays

The generic backend supports renderer-level visual overlays without knowing template names:

- text via `drawtext`;
- image/video asset overlays;
- normalized placement;
- absolute timing enable windows;
- z-order from the already sorted render plan.

Text values and renderer style properties are escaped/validated before they reach the filtergraph. Template policy such as headline placement or wrapping remains upstream.

## Audio

PR5 compiles renderer-independent planned audio tracks into a deterministic audio graph. Supported primitives include:

- source audio from audio/video inputs;
- source offsets;
- bounded duration;
- looped tracks;
- gain adjustment;
- absolute timeline delay;
- deterministic mixing;
- synthesized silence when the plan has no audio.

Visual scene transitions do not invent hidden audio crossfade semantics. Audio behavior is driven only by explicit planned audio tracks.

## Command manifests

`RenderCommandManifest` contains the exact argv/filtergraph/input description passed to FFmpeg plus:

- backend version;
- render-plan digest;
- selected video encoder;
- output path;
- deterministic input ordering and roles.

`command_manifest_digest()` hashes canonical JSON for reproducibility and later artifact/QC manifests.

The backend uses argv sequences with `shell=False`; it does not construct a shell command string.

## Execution

`execute_ffmpeg()` runs a compiled command with:

- structured errors;
- bounded stderr capture;
- optional timeout;
- cancellation support;
- cleanup of partial output on failure/cancellation;
- output existence/non-empty verification.

PR7 adds the persistent orchestration layer around this generic backend; the backend itself remains unaware of jobs, templates, review state, or publishing.
