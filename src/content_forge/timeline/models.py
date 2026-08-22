"""Renderer-independent normalized timeline and render-plan models."""

from __future__ import annotations

from typing import Mapping, Self

from pydantic import Field, JsonValue, model_validator

from content_forge.core import (
    AudioTrack,
    FitMode,
    MediaType,
    NormalizedPoint,
    NormalizedRect,
    OutputProfile,
    Overlay,
    RegistryKey,
    Scene,
)
from content_forge.core.models import FrozenModel

RENDER_PLAN_VERSION = "1.0"
TIMELINE_COMPILER_VERSION = "1"
_EPSILON = 1e-9


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= _EPSILON


class ResolvedTemplate(FrozenModel):
    """Renderer-independent contribution produced by an upstream template resolver.

    `scenes=None` means the canonical project scenes pass through unchanged. A template
    may instead provide a fully resolved replacement scene graph, which is how future
    formats can change placement/repetition without adding content-specific branches to
    the timeline compiler or renderer.
    """

    template_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)
    scenes: tuple[Scene, ...] | None = None
    overlays: tuple[Overlay, ...] = ()
    audio_tracks: tuple[AudioTrack, ...] = ()
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

    @model_validator(mode="after")
    def validate_transition_semantics(self) -> Self:
        if self.transition_type == "cut":
            if self.duration_seconds > _EPSILON:
                raise ValueError("planned cut transition must have zero duration")
        elif self.duration_seconds <= 0.0:
            raise ValueError("planned non-cut transition must have positive duration")
        return self


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

    @model_validator(mode="after")
    def validate_time_identity(self) -> Self:
        if not _close(self.end_seconds, self.start_seconds + self.duration_seconds):
            raise ValueError("planned scene end must equal start + duration")
        return self


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

    @model_validator(mode="after")
    def validate_time_identity(self) -> Self:
        if not _close(self.end_seconds, self.start_seconds + self.duration_seconds):
            raise ValueError("planned overlay end must equal start + duration")
        return self


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

    @model_validator(mode="after")
    def validate_time_identity(self) -> Self:
        if not _close(self.end_seconds, self.start_seconds + self.duration_seconds):
            raise ValueError("planned audio end must equal start + duration")
        return self


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

    @model_validator(mode="after")
    def validate_plan_graph(self) -> Self:
        if not self.scenes:
            raise ValueError("render plan requires at least one scene")

        orders = tuple(scene.order for scene in self.scenes)
        if orders != tuple(range(len(self.scenes))):
            raise ValueError("render plan scene orders must be contiguous from zero")
        scene_ids = [scene.scene_id for scene in self.scenes]
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError("render plan scene IDs must be unique")

        first = self.scenes[0]
        last = self.scenes[-1]
        if not _close(first.start_seconds, 0.0):
            raise ValueError("render plan first scene must start at zero")
        if first.transition_in.transition_type != "cut":
            raise ValueError("render plan first scene transition_in must be cut")
        if last.transition_out.transition_type != "cut":
            raise ValueError("render plan last scene transition_out must be cut")

        for index, scene in enumerate(self.scenes):
            incoming = scene.transition_in.duration_seconds
            outgoing = scene.transition_out.duration_seconds
            if incoming + outgoing - scene.duration_seconds > _EPSILON:
                raise ValueError(
                    "render plan transition overlaps consume more than scene duration"
                )

            if index == 0:
                continue
            previous = self.scenes[index - 1]
            if previous.transition_out != scene.transition_in:
                raise ValueError("render plan adjacent scene transitions must agree")
            transition_duration = scene.transition_in.duration_seconds
            if transition_duration - previous.duration_seconds > _EPSILON:
                raise ValueError("render plan transition exceeds preceding scene duration")
            if transition_duration - scene.duration_seconds > _EPSILON:
                raise ValueError("render plan transition exceeds following scene duration")
            expected_start = previous.end_seconds - transition_duration
            if not _close(scene.start_seconds, expected_start):
                raise ValueError(
                    "render plan scene start must equal previous end minus transition"
                )

        if not _close(self.total_duration_seconds, last.end_seconds):
            raise ValueError("render plan duration must equal final scene end")

        overlay_ids = [item.overlay_id for item in self.overlays]
        if len(overlay_ids) != len(set(overlay_ids)):
            raise ValueError("render plan overlay IDs must be unique")
        audio_ids = [item.audio_track_id for item in self.audio_tracks]
        if len(audio_ids) != len(set(audio_ids)):
            raise ValueError("render plan audio IDs must be unique")
        asset_ids = [item.asset_id for item in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("render plan asset IDs must be unique")

        scene_by_id = {scene.scene_id: scene for scene in self.scenes}
        asset_id_set = set(asset_ids)
        for overlay in self.overlays:
            if overlay.end_seconds - self.total_duration_seconds > _EPSILON:
                raise ValueError("planned overlay extends past render-plan duration")
            if overlay.scope_scene_id is not None:
                scoped_scene = scene_by_id.get(overlay.scope_scene_id)
                if scoped_scene is None:
                    raise ValueError("planned overlay references unknown scene scope")
                if overlay.start_seconds + _EPSILON < scoped_scene.start_seconds:
                    raise ValueError("planned overlay starts before its scene scope")
                if overlay.end_seconds - scoped_scene.end_seconds > _EPSILON:
                    raise ValueError("planned overlay extends past its scene scope")
            if overlay.asset_id is not None and overlay.asset_id not in asset_id_set:
                raise ValueError("planned overlay references missing planned asset")
        for track in self.audio_tracks:
            if track.end_seconds - self.total_duration_seconds > _EPSILON:
                raise ValueError("planned audio extends past render-plan duration")
            if track.scope_scene_id is not None:
                scoped_scene = scene_by_id.get(track.scope_scene_id)
                if scoped_scene is None:
                    raise ValueError("planned audio references unknown scene scope")
                if track.start_seconds + _EPSILON < scoped_scene.start_seconds:
                    raise ValueError("planned audio starts before its scene scope")
                if track.end_seconds - scoped_scene.end_seconds > _EPSILON:
                    raise ValueError("planned audio extends past its scene scope")
            if track.asset_id is not None and track.asset_id not in asset_id_set:
                raise ValueError("planned audio references missing planned asset")
        for scene in self.scenes:
            if scene.media_asset_id is not None and scene.media_asset_id not in asset_id_set:
                raise ValueError("planned scene references missing planned asset")
        return self
