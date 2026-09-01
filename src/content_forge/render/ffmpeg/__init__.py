"""FFmpeg/ffprobe backend."""

from .audio_compiler import compile_audio_intermediate_command
from .backend import FFmpegBackend
from .capabilities import (
    FFmpegCapabilityError,
    parse_encoders,
    parse_filters,
    probe_ffmpeg_runtime,
    require_filters,
    select_h264_encoder,
)
from .compiler import (
    AssetPathSource,
    FFmpegCompileError,
    MissingRenderAssetError,
    RuntimeStorageResolver,
    UnsupportedRenderFeatureError,
)
from .geometry import RenderGeometryError, resolve_pixel_rect
from .models import (
    FFmpegCapabilities,
    MediaProbe,
    PixelRect,
    RenderCommandManifest,
    RenderError,
    RenderInput,
    RenderResult,
    command_manifest_digest,
)
from .presentation_compiler import compile_ffmpeg_command
from .probe import MediaProbeError, apply_probe_to_asset, probe_media
from .runner import CancellationToken, FFmpegBackendError, execute_ffmpeg

__all__ = [
    "AssetPathSource",
    "CancellationToken",
    "FFmpegBackend",
    "FFmpegBackendError",
    "FFmpegCapabilities",
    "FFmpegCapabilityError",
    "FFmpegCompileError",
    "MediaProbe",
    "MediaProbeError",
    "MissingRenderAssetError",
    "PixelRect",
    "RenderCommandManifest",
    "RenderError",
    "RenderGeometryError",
    "RenderInput",
    "RenderResult",
    "RuntimeStorageResolver",
    "UnsupportedRenderFeatureError",
    "apply_probe_to_asset",
    "command_manifest_digest",
    "compile_audio_intermediate_command",
    "compile_ffmpeg_command",
    "execute_ffmpeg",
    "parse_encoders",
    "parse_filters",
    "probe_ffmpeg_runtime",
    "probe_media",
    "require_filters",
    "resolve_pixel_rect",
    "select_h264_encoder",
]
