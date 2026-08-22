"""Renderer-independent normalized timeline and render-plan models."""

from __future__ import annotations

from typing import Mapping

from pydantic import Field, JsonValue

from content_forge.core import (
    FitMode,
    MediaType,
    NormalizedPoint,
    NormalizedRect,
    OutputProfile,
    RegistryKey,
)
from content_forge.core.models import FrozenModel

RENDER_PLAN_VERSION = "1.0"
TIMELINE_COMPILER_VERSION = "1"


class ResolvedTemplate(FrozenModel):
    """Renderer-independent contribution produced by an upstream template resolver.

    PR4 deliberately does not implement a template registry. Later template plugins may
    resolve arbitrary content-specific logic into these ordinary overlays/audio tracks
    before the deterministic timeline compiler runs.
    """

    template_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)
    overlays: tuple[object, ...] = ()
    audio_tracks: tuple[object, ...] = ()
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)


class PlannedAsset(FrozenModel):
    asset_id: str
    sha256: str
    media_type: MediaType
    mime_type: str
    storage_key: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    has_audio: bool | None = None


class PlannedTransition(FrozenModel):
    transition_type: RegistryKey = "cut"
    duration_seconds: float = Field(default=0.0, ge=0.0)
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)


class PlannedScene(FrozenModel):
    scene_id: str
    order: int = Field(ge=0)
    start_seconds: float = Field(ge=0.0)
    duration_seconds: float = Field(gt=0.0)
    end_seconds: float = Field(gt=0.0)
    media_asset_id: str | None = None
    media_source_id: str | None = None
    trim_start_seconds: float = Field(default=0.0, ge=0.0)
    trim_duration_seconds: float | None = Field(default=None, gt=0.0)
    placement: NormalizedRect
    fit_mode: FitMode
    crop: NormalizedRect | None = None
    focus: NormalizedPoint | None = None
    motion_type: RegistryKey | None = None
    motion_start_rect: NormalizedRect | None = None
    motion_end_rect: NormalizedRect | None = None
    motion_focus: NormalizedPoint | None = None
    motion_properties: Mapping[str, JsonValue] = Field(default_factory=dict)
    transition_in: PlannedTransition = PlannedTransition()
    transition_out: PlannedTransition = PlannedTransition()
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)


class PlannedOverlay(FrozenModel):
    overlay_id: str
    component_type: RegistryKey
    scope_scene_id: str | None = None
    start_seconds: float = Field(ge=0.0)
    duration_seconds: float = Field(gt=0.0)
    end_seconds: float = Field(gt=0.0)
    placement: NormalizedRect
    z_index: int = 0
    text: str | None = None
    asset_id: str | None = None
    source_id: str | None = None
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)


class PlannedAudioTrack(FrozenModel):
    audio_track_id: str
    track_type: RegistryKey
    scope_scene_id: str | None = None
    start_seconds: float = Field(ge=0.0)
    duration_seconds: float = Field(gt=0.0)
    end_seconds: float = Field(gt=0.0)
    asset_id: str | None = None
    source_id: str | None = None
    source_start_seconds: float = Field(default=0.0, ge=0.0)
    gain_db: float = Field(ge=-120.0, le=24.0)
    loop: bool = False
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)


class RenderPlan(FrozenModel):
    """Concrete semantic plan consumed by later render backends.

    Geometry remains normalized and profile-independent. Pixel dimensions are carried
    only by `output_profile`; PR5 resolves normalized rectangles to backend pixels.
    """

    render_plan_version: str = RENDER_PLAN_VERSION
    compiler_version: str = TIMELINE_COMPILER_VERSION
    project_id: str
    variant_id: str | None = None
    variant_language: str | None = None
    template_id: str | None = None
    template_version: str | None = None
    output_profile: OutputProfile
    total_duration_seconds: float = Field(gt=0.0)
    scenes: tuple[PlannedScene, ...]
    overlays: tuple[PlannedOverlay, ...] = ()
    audio_tracks: tuple[PlannedAudioTrack, ...] = ()
    assets: tuple[PlannedAsset, ...] = ()
    template_properties: Mapping[str, JsonValue] = Field(default_factory=dict)
