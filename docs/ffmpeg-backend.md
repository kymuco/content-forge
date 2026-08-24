# FFmpeg / ffprobe backend

PR5 is the first executable render backend for Content Forge. It consumes the normalized, renderer-independent `RenderPlan` produced by PR4 and emits a deterministic FFmpeg command manifest before any subprocess is started.

```text
RenderPlan
    ↓
normalized geometry → output-profile pixels
    ↓
FFmpeg filtergraph + argv compiler
    ↓
RenderCommandManifest
    ↓
FFmpeg process runner
    ↓
MP4
```

The backend does not inspect `content_kind`, workflow IDs, or named templates. Content-specific decisions remain upstream.

## Runtime capability probe

`probe_ffmpeg_runtime()` resolves `ffmpeg` and `ffprobe`, records their version strings, encoder list, and filter list, and separately tests NVENC runtime usability.

The presence of `h264_nvenc` in `ffmpeg -encoders` is not enough. That only says the FFmpeg build contains the encoder. The machine may still lack a usable NVIDIA device, driver, or session. When enabled, PR5 performs a one-frame synthetic NVENC encode. `h264_nvenc` is selected only if that probe succeeds.

Encoder policy:

```text
prefer NVENC + runtime probe succeeds → h264_nvenc
otherwise libx264 exists             → libx264
otherwise                             → fail closed
```

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

- image and video scene media;
- explicit source trims for video;
- normalized source crop;
- `cover`;
- `contain`;
- `stretch`;
- `blur_background`;
- arbitrary normalized placement on a black canvas;
- cuts;
- a bounded set of FFmpeg `xfade` transitions.

`cover` uses focus coordinates when PR4 supplied them. Motion is currently rejected rather than ignored; later motion work can extend the backend without changing RenderPlan semantics.

## Visual overlays and text

Planned visual assets are resolved as additional FFmpeg inputs and overlaid by absolute timeline timing and z-index.

PR5 also exposes a deliberately primitive `drawtext` path. It supports concrete text, resolved placement, font size/color, border, and optional box styling. It is **not** the final headline layout engine: wrapping, typography policy, safe-zone text fitting, and the actual `hook_overlay` template belong to PR6.

Unknown/non-renderable overlay semantics fail closed.

## Audio

PR5 maps every planned audio source into absolute timeline time using the source offset already frozen by PR4. The primitive path supports:

- source trim/offset;
- looping input;
- gain in dB;
- stereo 48 kHz normalization;
- multi-track mix;
- AAC output.

A deterministic silent base from time zero prevents a first late-starting track from shifting the mixed timeline origin.

## Asset path resolution

The command compiler accepts either:

- `asset_id -> local path` mapping; or
- an object implementing `resolve(PlannedAsset)`.

`RuntimeStorageResolver` bridges this to PR3 `RuntimePaths`. It verifies that a planned asset's `storage_key` matches the canonical SHA-256 content-addressed key before resolving the blob.

Paths are passed as argv entries; Content Forge never builds a shell command string.

## Deterministic command manifests

Before execution the backend creates `RenderCommandManifest`, containing:

- backend/manifest version;
- PR4 render-plan digest;
- exact FFmpeg executable;
- selected encoder;
- filtergraph;
- ordered argv;
- ordered input records;
- output path;
- stable metadata.

`command_manifest_digest()` hashes strict sorted JSON. No timestamps are included.

This provides reproducibility evidence and gives future jobs/cache/QC layers a concrete boundary without parsing shell strings.

## Cancellation and failures

`execute_ffmpeg()` uses `subprocess.Popen(..., shell=False)` and supports:

- cancellation token;
- optional timeout;
- graceful terminate followed by kill fallback;
- deletion of partial output on failure/cancellation;
- output existence/non-empty verification;
- structured `RenderError` payloads.

Primary error codes currently include:

```text
ffmpeg_start_failed
ffmpeg_failed
render_cancelled
render_timeout
render_output_missing
```

Compile/capability failures occur before process execution and use their own typed exceptions.

## Synthetic tests

Repository tests contain no copyrighted or production media. The integration exit test writes a tiny synthetic PPM image, compiles it through the real PR4 → PR5 boundary, renders it to H.264 MP4 with the CPU fallback, and verifies the result with ffprobe when FFmpeg is installed.

The tests skip the real-runtime fixture when FFmpeg/libx264 is unavailable; deterministic compiler, capability parser, geometry, process runner, cancellation, and structured-error tests remain runnable without media assets or GPU hardware.

## Deliberate PR5 limits

PR5 is the generic backend, not the first finished Short template. It does not yet own:

- headline wrapping/typography layout;
- font asset policy;
- YouTube safe-zone layout decisions;
- high-level template registry;
- advanced motion rendering;
- subtitle/ASS rendering;
- loudness normalization policy;
- proxy/thumbnail generation;
- publishing or QC orchestration.

PR6 builds `hook_overlay` on top of this backend without adding template-specific branches to it.
