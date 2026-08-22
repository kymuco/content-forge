"""Validated contracts for the FFmpeg backend."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Mapping, Self

from pydantic import Field, JsonValue, model_validator

from content_forge.core import MediaType
from content_forge.core.models import FrozenModel

COMMAND_MANIFEST_VERSION = "1.0"
FFMPEG_BACKEND_VERSION = "1"
CommandManifestVersion = Literal["1.0"]
FFmpegBackendVersion = Literal["1"]


def paths_alias(left: str | Path, right: str | Path) -> bool:
    """Return whether two local paths identify the same filesystem object.

    Resolved-path equality catches lexical and symlink aliases. `samefile()` additionally
    catches hard links when both paths exist. Failure to stat a path is treated as
    non-aliasing after the canonical comparison so a not-yet-created output is valid.
    """

    left_path = Path(left).expanduser().resolve()
    right_path = Path(right).expanduser().resolve()
    if left_path == right_path:
        return True
    try:
        return left_path.exists() and right_path.exists() and left_path.samefile(right_path)
    except OSError:
        return False


class FFmpegCapabilities(FrozenModel):
    ffmpeg_path: str = Field(min_length=1)
    ffprobe_path: str = Field(min_length=1)
    ffmpeg_version: str = Field(min_length=1)
    ffprobe_version: str = Field(min_length=1)
    encoders: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    h264_nvenc_usable: bool = False
    nvenc_probe_error: str | None = None

    @property
    def has_h264_nvenc(self) -> bool:
        return "h264_nvenc" in self.encoders

    @property
    def has_libx264(self) -> bool:
        return "libx264" in self.encoders


class MediaProbe(FrozenModel):
    path: str = Field(min_length=1)
    format_name: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0.0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    fps: float | None = Field(default=None, gt=0.0)
    has_video: bool = False
    has_audio: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None


class PixelRect(FrozenModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class RenderInput(FrozenModel):
    input_index: int = Field(ge=0)
    asset_id: str
    path: str = Field(min_length=1)
    media_type: MediaType
    role: str = Field(min_length=1, max_length=128)
    loop: bool = False
    seek_seconds: float = Field(default=0.0, ge=0.0)
    duration_seconds: float | None = Field(default=None, gt=0.0)


class RenderCommandManifest(FrozenModel):
    command_manifest_version: CommandManifestVersion = COMMAND_MANIFEST_VERSION
    backend_version: FFmpegBackendVersion = FFMPEG_BACKEND_VERSION
    render_plan_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    ffmpeg_path: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    video_encoder: str = Field(min_length=1)
    filtergraph: str = Field(min_length=1)
    arguments: tuple[str, ...]
    inputs: tuple[RenderInput, ...] = ()
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if not self.arguments:
            raise ValueError("FFmpeg command manifest requires arguments")
        if self.arguments[-1] != self.output_path:
            raise ValueError("FFmpeg output path must be the final command argument")
        input_indices = [item.input_index for item in self.inputs]
        if input_indices != list(range(len(self.inputs))):
            raise ValueError("FFmpeg input indices must be contiguous from zero")
        for item in self.inputs:
            if paths_alias(self.output_path, item.path):
                raise ValueError(
                    "FFmpeg output path must not alias any render input path"
                )
        return self

    @property
    def command(self) -> tuple[str, ...]:
        return (self.ffmpeg_path, *self.arguments)


class RenderError(FrozenModel):
    code: str = Field(min_length=1, max_length=128)
    stage: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8192)
    return_code: int | None = None
    stderr_tail: str | None = Field(default=None, max_length=16384)
    manifest_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    details: Mapping[str, JsonValue] = Field(default_factory=dict)


class RenderResult(FrozenModel):
    output_path: str = Field(min_length=1)
    bytes_written: int = Field(ge=1)
    elapsed_seconds: float = Field(ge=0.0)
    manifest_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    return_code: int = 0


def command_manifest_digest(manifest: RenderCommandManifest) -> str:
    payload = manifest.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_output_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())
