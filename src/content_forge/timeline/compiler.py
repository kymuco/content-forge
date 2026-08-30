"""Deterministic compiler from canonical projects to renderer-independent plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol

from content_forge.core import (
    Asset,
    AudioTrack,
    MediaType,
    OutputProfile,
    Overlay,
    Project,
    Scene,
    TransitionSpec,
    Variant,
)

from .models import (
    PlannedAsset,
    PlannedAudioTrack,
    PlannedOverlay,
    PlannedScene,
    PlannedTransition,
    RenderPlan,
    ResolvedTemplate,
)

_EPSILON = 1e-9
_BUILTIN_VARIANT_FIELDS = {"hook", "title", "description"}


class TimelineCompileError(ValueError):
    """Base error for semantic timeline compilation failures."""


class MissingTimelineAssetError(TimelineCompileError):
    pass


class TimelineBoundsError(TimelineCompileError):
    pass


class TimelineTransitionError(TimelineCompileError):
    pass


class TimelineSelectionError(TimelineCompileError):
    pass


class TemplateResolutionError(TimelineCompileError):
    pass


class AssetResolver(Protocol):
    def get_asset(self, asset_id: str) -> Asset | None: ...


AssetSource = Mapping[str, Asset] | AssetResolver


def _seconds(value: float) -> float:
    """Canonicalize arithmetic noise while retaining sub-microsecond headroom."""

    return round(float(value), 9)


def _asset_from(source: AssetSource, asset_id: str) -> Asset | None:
    if isinstance(source, Mapping):
        return source.get(asset_id)
    return source.get_asset(asset_id)


def _select_profile(project: Project, profile_id: str | None) -> OutputProfile:
    if profile_id is not None:
        for profile in project.output_profiles:
            if profile.profile_id == profile_id:
                return profile
        raise TimelineSelectionError(f"unknown output profile: {profile_id}")
    if len(project.output_profiles) != 1:
        raise TimelineSelectionError(
            "profile_id is required unless the project has exactly one output profile"
        )
    return project.output_profiles[0]


def _select_variant(project: Project, variant_id: str | None) -> Variant | None:
    if variant_id is not None:
        for variant in project.variants:
            if variant.variant_id == variant_id:
                return variant
        raise TimelineSelectionError(f"unknown variant: {variant_id}")
    if not project.variants:
        return None
    if len(project.variants) == 1:
        return project.variants[0]
    raise TimelineSelectionError(
        "variant_id is required when the project has more than one variant"
    )


def _validate_template(
    project: Project,
    template: ResolvedTemplate | None,
    *,
    profile: OutputProfile,
    variant: Variant | None,
) -> ResolvedTemplate | None:
    if project.template is None:
        if template is not None:
            raise TemplateResolutionError(
                "resolved template supplied for a project with no template reference"
            )
        return None
    if template is None:
        raise TemplateResolutionError(
            "project template must be resolved before timeline compilation"
        )
    if template.template_id != project.template.template_id:
        raise TemplateResolutionError(
            "resolved template ID does not match project template reference"
        )
    if template.version != project.template.version:
        raise TemplateResolutionError(
            "resolved template version does not match project template reference"
        )

    if "resolved_profile_id" in template.properties:
        resolved_profile_id = template.properties["resolved_profile_id"]
        if not isinstance(resolved_profile_id, str):
            raise TemplateResolutionError(
                "resolved template profile binding must be a string"
            )
        if resolved_profile_id != profile.profile_id:
            raise TemplateResolutionError(
                "resolved template profile binding does not match selected output profile"
            )

    if "resolved_variant_id" in template.properties:
        resolved_variant_id = template.properties["resolved_variant_id"]
        selected_variant_id = None if variant is None else variant.variant_id
        if resolved_variant_id is not None and not isinstance(resolved_variant_id, str):
            raise TemplateResolutionError(
                "resolved template variant binding must be a string or null"
            )
        if resolved_variant_id != selected_variant_id:
            raise TemplateResolutionError(
                "resolved template variant binding does not match selected variant"
            )

    return template


def _planned_transition(spec: TransitionSpec | None) -> PlannedTransition:
    if spec is None:
        return PlannedTransition()
    if spec.transition_type == "cut":
        if abs(spec.duration_seconds) > _EPSILON:
            raise TimelineTransitionError("cut transition must have zero duration")
    elif spec.duration_seconds <= 0.0:
        raise TimelineTransitionError(
            "non-cut transition must have a positive duration"
        )
    return PlannedTransition(
        transition_type=spec.transition_type,
        duration_seconds=_seconds(spec.duration_seconds),
        properties=spec.properties,
    )


def _transition_pair(left: Scene, right: Scene) -> PlannedTransition:
    if left.transition_out is not None and right.transition_in is not None:
        left_plan = _planned_transition(left.transition_out)
        right_plan = _planned_transition(right.transition_in)
        if left_plan != right_plan:
            raise TimelineTransitionError(
                f"scene transition disagreement between {left.scene_id} and {right.scene_id}"
            )
        return left_plan
    if left.transition_out is not None:
        return _planned_transition(left.transition_out)
    if right.transition_in is not None:
        return _planned_transition(right.transition_in)
    return PlannedTransition()


def _scene_schedule(scenes: tuple[Scene, ...]) -> tuple[
    tuple[float, ...], tuple[float, ...], tuple[PlannedTransition, ...]
]:
    if not scenes:
        raise TimelineCompileError("project must contain at least one scene")
    ordered = tuple(sorted(scenes, key=lambda item: item.order))
    scene_ids = [scene.scene_id for scene in ordered]
    if len(scene_ids) != len(set(scene_ids)):
        raise TimelineCompileError("scene IDs must be unique after template resolution")

    expected_orders = tuple(range(len(ordered)))
    actual_orders = tuple(scene.order for scene in ordered)
    if actual_orders != expected_orders:
        raise TimelineCompileError(
            f"scene orders must be contiguous from zero; got {actual_orders}"
        )

    first_boundary = _planned_transition(ordered[0].transition_in)
    if first_boundary.duration_seconds > 0.0 or first_boundary.transition_type != "cut":
        raise TimelineTransitionError("first scene cannot have a non-cut transition_in")
    last_boundary = _planned_transition(ordered[-1].transition_out)
    if last_boundary.duration_seconds > 0.0 or last_boundary.transition_type != "cut":
        raise TimelineTransitionError("last scene cannot have a non-cut transition_out")

    transitions = tuple(
        _transition_pair(ordered[index], ordered[index + 1])
        for index in range(len(ordered) - 1)
    )
    for index, transition in enumerate(transitions):
        if transition.duration_seconds - ordered[index].duration_seconds > _EPSILON:
            raise TimelineTransitionError("transition exceeds preceding scene duration")
        if transition.duration_seconds - ordered[index + 1].duration_seconds > _EPSILON:
            raise TimelineTransitionError("transition exceeds following scene duration")

    for index, scene in enumerate(ordered):
        incoming = transitions[index - 1].duration_seconds if index > 0 else 0.0
        outgoing = transitions[index].duration_seconds if index < len(transitions) else 0.0
        if incoming + outgoing - scene.duration_seconds > _EPSILON:
            raise TimelineTransitionError(
                f"transition overlaps consume more than scene duration: {scene.scene_id}"
            )

    starts: list[float] = [0.0]
    ends: list[float] = [_seconds(ordered[0].duration_seconds)]
    for index in range(1, len(ordered)):
        start = _seconds(ends[index - 1] - transitions[index - 1].duration_seconds)
        end = _seconds(start + ordered[index].duration_seconds)
        starts.append(start)
        ends.append(end)
    return tuple(starts), tuple(ends), transitions


def _resolve_variant_text(overlay: Overlay, variant: Variant | None) -> str | None:
    if overlay.variant_field is None:
        return overlay.text

    key = str(overlay.variant_field)
    if variant is not None:
        if key in variant.text_overrides:
            return variant.text_overrides[key]
        if key in _BUILTIN_VARIANT_FIELDS:
            value = getattr(variant, key)
            if value is not None:
                return value
    if overlay.text is not None:
        return overlay.text
    raise TimelineCompileError(
        f"overlay {overlay.overlay_id} could not resolve variant field {key!r}"
    )


def compile_timeline(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
    template: ResolvedTemplate | None = None,
) -> RenderPlan:
    """Compile a project into a deterministic, renderer-independent render plan."""

    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id)
    resolved_template = _validate_template(
        project,
        template,
        profile=profile,
        variant=variant,
    )

    source_scenes = (
        project.scenes
        if resolved_template is None or resolved_template.scenes is None
        else resolved_template.scenes
    )
    ordered_scenes = tuple(sorted(source_scenes, key=lambda item: item.order))
    starts, ends, transitions = _scene_schedule(ordered_scenes)
    total_duration = ends[-1]

    asset_cache: dict[str, Asset] = {}

    def require_asset(asset_id: str) -> Asset:
        cached = asset_cache.get(asset_id)
        if cached is not None:
            return cached
        asset = _asset_from(assets, asset_id)
        if asset is None:
            raise MissingTimelineAssetError(f"unknown asset in timeline: {asset_id}")
        if asset.asset_id != asset_id:
            raise TimelineCompileError(
                f"asset resolver returned {asset.asset_id} for requested {asset_id}"
            )
        asset_cache[asset_id] = asset
        return asset

    planned_scenes: list[PlannedScene] = []
    for index, scene in enumerate(ordered_scenes):
        if scene.media is None:
            if (
                scene.trim_start_seconds > _EPSILON
                or scene.trim_duration_seconds is not None
            ):
                raise TimelineCompileError(
                    "scene without media cannot define source trim"
                )
        else:
            media_asset = require_asset(scene.media.asset_id)
            if media_asset.media_type not in {MediaType.VIDEO, MediaType.IMAGE}:
                raise TimelineCompileError(
                    f"scene media must be video or image: {media_asset.asset_id}"
                )
            if media_asset.media_type is MediaType.IMAGE:
                if scene.trim_start_seconds > _EPSILON or scene.trim_duration_seconds is not None:
                    raise TimelineCompileError("image scenes cannot define source trim")
            else:
                requested = (
                    scene.trim_duration_seconds
                    if scene.trim_duration_seconds is not None
                    else scene.duration_seconds
                )
                if requested + _EPSILON < scene.duration_seconds:
                    raise TimelineBoundsError(
                        f"scene source trim is shorter than scene duration: {scene.scene_id}"
                    )
                if (
                    media_asset.duration_seconds is not None
                    and scene.trim_start_seconds + requested
                    - media_asset.duration_seconds
                    > _EPSILON
                ):
                    raise TimelineBoundsError(
                        f"scene source trim exceeds asset duration: {scene.scene_id}"
                    )

        motion = scene.motion
        planned_scenes.append(
            PlannedScene(
                scene_id=scene.scene_id,
                order=scene.order,
                start_seconds=starts[index],
                duration_seconds=_seconds(scene.duration_seconds),
                end_seconds=ends[index],
                media_asset_id=None if scene.media is None else scene.media.asset_id,
                media_source_id=None if scene.media is None else scene.media.source_id,
                trim_start_seconds=_seconds(scene.trim_start_seconds),
                trim_duration_seconds=(
                    None
                    if scene.trim_duration_seconds is None
                    else _seconds(scene.trim_duration_seconds)
                ),
                placement=scene.placement,
                fit_mode=scene.fit_mode,
                crop=scene.crop,
                focus=scene.focus,
                motion_type=None if motion is None else motion.motion_type,
                motion_start_rect=None if motion is None else motion.start_rect,
                motion_end_rect=None if motion is None else motion.end_rect,
                motion_focus=None if motion is None else motion.focus,
                motion_properties={} if motion is None else motion.properties,
                transition_in=(
                    PlannedTransition() if index == 0 else transitions[index - 1]
                ),
                transition_out=(
                    PlannedTransition()
                    if index == len(ordered_scenes) - 1
                    else transitions[index]
                ),
                properties=scene.properties,
            )
        )

    global_overlays: list[Overlay] = list(project.overlays)
    global_audio: list[AudioTrack] = list(project.audio_tracks)
    if resolved_template is not None:
        global_overlays.extend(resolved_template.overlays)
        global_audio.extend(resolved_template.audio_tracks)

    overlay_ids: set[str] = set()
    audio_ids: set[str] = set()
    planned_overlays: list[PlannedOverlay] = []
    planned_audio: list[PlannedAudioTrack] = []

    def add_overlay(
        overlay: Overlay,
        *,
        scope_start: float,
        scope_duration: float,
        scope_scene_id: str | None,
    ) -> None:
        if overlay.overlay_id in overlay_ids:
            raise TimelineCompileError(
                f"duplicate overlay ID after template resolution: {overlay.overlay_id}"
            )
        overlay_ids.add(overlay.overlay_id)
        if overlay.placement is None:
            raise TimelineCompileError(
                f"overlay must have resolved normalized placement: {overlay.overlay_id}"
            )
        if overlay.start_seconds + _EPSILON >= scope_duration:
            raise TimelineBoundsError(
                f"overlay starts outside its timeline scope: {overlay.overlay_id}"
            )
        local_duration = (
            scope_duration - overlay.start_seconds
            if overlay.duration_seconds is None
            else overlay.duration_seconds
        )
        if overlay.start_seconds + local_duration - scope_duration > _EPSILON:
            raise TimelineBoundsError(
                f"overlay exceeds its timeline scope: {overlay.overlay_id}"
            )
        asset_id = None
        source_id = None
        if overlay.asset_ref is not None:
            asset = require_asset(overlay.asset_ref.asset_id)
            if asset.media_type not in {MediaType.VIDEO, MediaType.IMAGE}:
                raise TimelineCompileError(
                    f"visual overlay requires a video or image asset: {asset.asset_id}"
                )
            asset_id = overlay.asset_ref.asset_id
            source_id = overlay.asset_ref.source_id
        start = _seconds(scope_start + overlay.start_seconds)
        duration = _seconds(local_duration)
        planned_overlays.append(
            PlannedOverlay(
                overlay_id=overlay.overlay_id,
                component_type=overlay.component_type,
                scope_scene_id=scope_scene_id,
                start_seconds=start,
                duration_seconds=duration,
                end_seconds=_seconds(start + duration),
                placement=overlay.placement,
                z_index=overlay.z_index,
                text=_resolve_variant_text(overlay, variant),
                asset_id=asset_id,
                source_id=source_id,
                properties=overlay.properties,
            )
        )

    def add_audio(
        track: AudioTrack,
        *,
        scope_start: float,
        scope_duration: float,
        scope_scene: Scene | None,
    ) -> None:
        if track.audio_track_id in audio_ids:
            raise TimelineCompileError(
                f"duplicate audio ID after template resolution: {track.audio_track_id}"
            )
        audio_ids.add(track.audio_track_id)
        if track.start_seconds + _EPSILON >= scope_duration:
            raise TimelineBoundsError(
                f"audio track starts outside its timeline scope: {track.audio_track_id}"
            )
        local_duration = (
            scope_duration - track.start_seconds
            if track.duration_seconds is None
            else track.duration_seconds
        )
        if track.start_seconds + local_duration - scope_duration > _EPSILON:
            raise TimelineBoundsError(
                f"audio track exceeds its timeline scope: {track.audio_track_id}"
            )

        ref = track.asset_ref
        source_start = 0.0
        if ref is None and track.track_type == "original":
            if scope_scene is None or scope_scene.media is None:
                raise TimelineCompileError(
                    "original audio requires a scene media asset"
                )
            ref = scope_scene.media
            source_start = scope_scene.trim_start_seconds + track.start_seconds

        asset_id = None
        source_id = None
        if ref is not None:
            asset = require_asset(ref.asset_id)
            asset_id = ref.asset_id
            source_id = ref.source_id
            if (
                asset.media_type not in {MediaType.AUDIO, MediaType.VIDEO}
                or asset.has_audio is False
            ):
                raise TimelineCompileError(
                    f"audio track references an asset with no audio: {ref.asset_id}"
                )
            if (
                not track.loop
                and asset.duration_seconds is not None
                and source_start + local_duration - asset.duration_seconds > _EPSILON
            ):
                raise TimelineBoundsError(
                    f"audio track exceeds source duration: {track.audio_track_id}"
                )

        start = _seconds(scope_start + track.start_seconds)
        duration = _seconds(local_duration)
        planned_audio.append(
            PlannedAudioTrack(
                audio_track_id=track.audio_track_id,
                track_type=track.track_type,
                scope_scene_id=None if scope_scene is None else scope_scene.scene_id,
                start_seconds=start,
                duration_seconds=duration,
                end_seconds=_seconds(start + duration),
                asset_id=asset_id,
                source_id=source_id,
                source_start_seconds=_seconds(source_start),
                gain_db=track.gain_db,
                loop=track.loop,
                properties=track.properties,
            )
        )

    for overlay in global_overlays:
        add_overlay(
            overlay,
            scope_start=0.0,
            scope_duration=total_duration,
            scope_scene_id=None,
        )
    for track in global_audio:
        add_audio(
            track,
            scope_start=0.0,
            scope_duration=total_duration,
            scope_scene=None,
        )

    for index, scene in enumerate(ordered_scenes):
        for overlay in scene.overlays:
            add_overlay(
                overlay,
                scope_start=starts[index],
                scope_duration=scene.duration_seconds,
                scope_scene_id=scene.scene_id,
            )
        for track in scene.audio_tracks:
            add_audio(
                track,
                scope_start=starts[index],
                scope_duration=scene.duration_seconds,
                scope_scene=scene,
            )

    planned_assets = tuple(
        PlannedAsset(
            asset_id=asset.asset_id,
            sha256=asset.sha256,
            media_type=asset.media_type,
            mime_type=asset.mime_type,
            storage_key=asset.storage_key,
            width=asset.width,
            height=asset.height,
            duration_seconds=asset.duration_seconds,
            has_audio=asset.has_audio,
        )
        for asset in sorted(asset_cache.values(), key=lambda item: item.asset_id)
    )

    return RenderPlan(
        project_id=project.project_id,
        variant_id=None if variant is None else variant.variant_id,
        variant_language=None if variant is None else variant.language,
        template_id=(
            None if resolved_template is None else resolved_template.template_id
        ),
        template_version=(
            None if resolved_template is None else resolved_template.version
        ),
        output_profile=profile,
        total_duration_seconds=_seconds(total_duration),
        scenes=tuple(planned_scenes),
        overlays=tuple(
            sorted(
                planned_overlays,
                key=lambda item: (
                    item.start_seconds,
                    item.z_index,
                    item.overlay_id,
                ),
            )
        ),
        audio_tracks=tuple(
            sorted(
                planned_audio,
                key=lambda item: (item.start_seconds, item.audio_track_id),
            )
        ),
        assets=planned_assets,
        template_properties=(
            {}
            if resolved_template is None
            else resolved_template.model_dump(mode="json")["properties"]
        ),
    )


def render_plan_digest(plan: RenderPlan) -> str:
    """Return a stable SHA-256 digest of the semantic render plan."""

    payload = plan.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
