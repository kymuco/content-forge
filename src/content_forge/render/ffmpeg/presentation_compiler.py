"""PR23 presentation-aware wrapper over the stable PR14 FFmpeg audio compiler."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

from content_forge.core import FitMode, MediaType, NormalizedRect
from content_forge.timeline import PlannedAsset, PlannedScene, RenderPlan, render_plan_digest

from .audio_compiler import compile_ffmpeg_command as _compile_audio_ffmpeg_command
from .capabilities import require_filters
from .compiler import (
    AssetPathSource,
    FFmpegCompileError,
    MissingRenderAssetError,
    RuntimeStorageResolver,
    UnsupportedRenderFeatureError,
)
from .geometry import resolve_pixel_rect
from .models import FFmpegCapabilities, RenderCommandManifest

_FOCUS_MOTION_TYPE = "focus_zoom"
_DUCK_TRIGGER_TYPES = frozenset({"original", "dialogue", "narration", "voice"})


def _number(value: float) -> str:
    text = f"{float(value):.9f}".rstrip("0").rstrip(".")
    return text or "0"


def _numeric_property(
    value: object,
    *,
    name: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if value is None:
        result = default
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FFmpegCompileError(f"invalid PR23 presentation property {name}")
    else:
        result = float(value)
    if not math.isfinite(result):
        raise FFmpegCompileError(f"invalid PR23 presentation property {name}")
    if minimum is not None and result < minimum:
        raise FFmpegCompileError(f"PR23 presentation property {name} is below minimum")
    if maximum is not None and result > maximum:
        raise FFmpegCompileError(f"PR23 presentation property {name} exceeds maximum")
    return result


def _clear_focus_motion(scene: PlannedScene) -> PlannedScene:
    if scene.motion_type != _FOCUS_MOTION_TYPE:
        return scene
    return scene.validated_copy(
        update={
            "motion_type": None,
            "motion_start_rect": None,
            "motion_end_rect": None,
            "motion_focus": None,
            "motion_properties": {},
        }
    )


def _asset_for_scene(plan: RenderPlan, scene: PlannedScene) -> PlannedAsset:
    if scene.media_asset_id is None:
        raise UnsupportedRenderFeatureError(
            f"PR23 focus motion scene has no media asset: {scene.scene_id}"
        )
    for asset in plan.assets:
        if asset.asset_id == scene.media_asset_id:
            return asset
    raise MissingRenderAssetError(
        f"PR23 focus motion references missing planned asset: {scene.media_asset_id}"
    )


def _input_index_for_scene(manifest: RenderCommandManifest, scene_id: str) -> int:
    expected = f"scene:{scene_id}"
    candidates = [item.input_index for item in manifest.inputs if item.role == expected]
    if len(candidates) != 1:
        raise FFmpegCompileError(
            f"cannot identify unique FFmpeg input for PR23 focus scene: {scene_id}"
        )
    return candidates[0]


def _fit_part_index(parts: list[str], scene_index: int) -> int:
    suffix = f"[scene_fit_{scene_index}]"
    candidates = [index for index, part in enumerate(parts) if part.endswith(suffix)]
    if len(candidates) != 1:
        raise FFmpegCompileError(
            f"cannot identify unique fitted stream for PR23 scene {scene_index}"
        )
    return candidates[0]


def _audio_part_index(parts: list[str], audio_index: int) -> int:
    suffix = f"[audio_{audio_index}]"
    candidates = [index for index, part in enumerate(parts) if part.endswith(suffix)]
    if len(candidates) != 1:
        raise FFmpegCompileError(
            f"cannot identify unique fitted audio stream {audio_index}"
        )
    return candidates[0]


def _append_before_label(part: str, label: str, filters: list[str]) -> str:
    if not filters:
        return part
    if not part.endswith(label):
        raise FFmpegCompileError(f"unexpected filtergraph fragment for {label}")
    return part[: -len(label)] + "," + ",".join(filters) + label


def _interpolated(start: float, end: float, progress: str) -> str:
    return f"({_number(start)}+({_number(end - start)})*{progress})"


def _focus_crop(scene: PlannedScene) -> NormalizedRect | None:
    raw = scene.motion_properties.get("focus_crop")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise UnsupportedRenderFeatureError("focus_zoom focus_crop must be an object")
    try:
        return NormalizedRect.model_validate(dict(raw))
    except Exception as exc:
        raise UnsupportedRenderFeatureError("focus_zoom focus_crop is invalid") from exc


def _focus_zoom_part(
    plan: RenderPlan,
    scene: PlannedScene,
    *,
    scene_index: int,
    input_index: int,
    asset: PlannedAsset,
) -> str:
    if asset.media_type is not MediaType.IMAGE:
        raise UnsupportedRenderFeatureError("focus_zoom currently supports image scenes only")
    if asset.width is None or asset.height is None:
        raise UnsupportedRenderFeatureError("focus_zoom requires source width/height metadata")
    if scene.fit_mode is not FitMode.COVER:
        raise UnsupportedRenderFeatureError("focus_zoom requires cover fit semantics")
    if scene.crop is not None:
        raise UnsupportedRenderFeatureError(
            "focus_zoom cannot be combined with an additional canonical crop"
        )
    focus = scene.motion_focus
    if focus is None:
        raise UnsupportedRenderFeatureError("focus_zoom requires normalized focus geometry")

    start_scale = _numeric_property(
        scene.motion_properties.get("start_scale"),
        name="start_scale",
        default=1.0,
        minimum=1e-6,
        maximum=1.0,
    )
    end_scale = _numeric_property(
        scene.motion_properties.get("end_scale"),
        name="end_scale",
        default=0.90,
        minimum=1e-6,
        maximum=1.0,
    )
    if end_scale > start_scale + 1e-12:
        raise UnsupportedRenderFeatureError("focus_zoom must zoom inward or hold")

    placement = resolve_pixel_rect(scene.placement, plan.output_profile)
    target_aspect = (
        scene.placement.width * plan.output_profile.width
    ) / (scene.placement.height * plan.output_profile.height)
    source_aspect = asset.width / asset.height
    normalized_ratio = target_aspect / source_aspect

    hint = _focus_crop(scene)
    if hint is None:
        min_x = 0.0
        min_y = 0.0
        bound_width = 1.0
        bound_height = 1.0
    else:
        min_x = hint.x
        min_y = hint.y
        bound_width = hint.width
        bound_height = hint.height
        if not (
            hint.x - 1e-12 <= focus.x <= hint.x + hint.width + 1e-12
            and hint.y - 1e-12 <= focus.y <= hint.y + hint.height + 1e-12
        ):
            raise UnsupportedRenderFeatureError("focus_zoom focus lies outside focus_crop")

    max_width = min(bound_width, bound_height * normalized_ratio)
    if max_width <= 0.0:
        raise UnsupportedRenderFeatureError("focus_zoom has no usable source window")
    max_height = max_width / normalized_ratio
    if max_height - bound_height > 1e-9:
        raise UnsupportedRenderFeatureError("focus_zoom source window exceeds focus bounds")

    start_width = max_width * start_scale
    start_height = max_height * start_scale
    end_width = max_width * end_scale
    end_height = max_height * end_scale

    total_frames = max(2, int(round(scene.duration_seconds * plan.output_profile.fps)))
    denominator = total_frames - 1
    progress = f"min(n/{denominator},1)"
    width = _interpolated(start_width, end_width, progress)
    height = _interpolated(start_height, end_height, progress)
    max_x = min_x + bound_width
    max_y = min_y + bound_height
    x = (
        f"min(max({_number(focus.x)}-{width}/2,{_number(min_x)}),"
        f"{_number(max_x)}-{width})"
    )
    y = (
        f"min(max({_number(focus.y)}-{height}/2,{_number(min_y)}),"
        f"{_number(max_y)}-{height})"
    )
    duration = _number(scene.duration_seconds)
    fps = _number(plan.output_profile.fps)

    return (
        f"[{input_index}:v]"
        f"trim=duration={duration},setpts=PTS-STARTPTS,fps={fps},"
        "scale="
        f"w='ceil({placement.width}/{width})':"
        f"h='ceil({placement.height}/{height})':"
        "force_original_aspect_ratio=increase:eval=frame,"
        f"crop={placement.width}:{placement.height}:"
        f"x='in_w*{x}':y='in_h*{y}',"
        f"trim=duration={duration},setpts=PTS-STARTPTS"
        f"[scene_fit_{scene_index}]"
    )


def _duck_expression(plan: RenderPlan, ducked_index: int) -> str | None:
    target = plan.audio_tracks[ducked_index]
    intervals: list[tuple[float, float]] = []
    for track in plan.audio_tracks:
        if track.track_type not in _DUCK_TRIGGER_TYPES:
            continue
        start = max(target.start_seconds, track.start_seconds)
        end = min(target.end_seconds, track.end_seconds)
        if end - start > 1e-9:
            intervals.append((start, end))
    if not intervals:
        return None
    return "+".join(
        f"between(t,{_number(start)},{_number(end)})" for start, end in intervals
    )


def _rewrite_presentation(
    plan: RenderPlan,
    manifest: RenderCommandManifest,
    capabilities: FFmpegCapabilities,
) -> tuple[str, int, int]:
    parts = manifest.filtergraph.split(";")
    required: set[str] = set()
    focus_count = 0
    ambience_duck_count = 0

    for scene_index, scene in enumerate(plan.scenes):
        if scene.motion_type != _FOCUS_MOTION_TYPE:
            continue
        asset = _asset_for_scene(plan, scene)
        input_index = _input_index_for_scene(manifest, scene.scene_id)
        fit_index = _fit_part_index(parts, scene_index)
        parts[fit_index] = _focus_zoom_part(
            plan,
            scene,
            scene_index=scene_index,
            input_index=input_index,
            asset=asset,
        )
        focus_count += 1
        required.update({"crop", "fps", "scale", "setpts", "trim"})

    for index, track in enumerate(plan.audio_tracks):
        if track.track_type != "ambience":
            continue
        duck_db = _numeric_property(
            track.properties.get("duck_db"),
            name="duck_db",
            default=0.0,
            minimum=-60.0,
            maximum=0.0,
        )
        expression = _duck_expression(plan, index)
        if duck_db >= 0.0 or expression is None:
            continue
        part_index = _audio_part_index(parts, index)
        label = f"[audio_{index}]"
        parts[part_index] = _append_before_label(
            parts[part_index],
            label,
            [f"volume={_number(duck_db)}dB:enable='{expression}'"],
        )
        ambience_duck_count += 1
        required.add("volume")

    if required:
        require_filters(capabilities, required)
    return ";".join(parts), focus_count, ambience_duck_count


def _replace_filter_complex(
    arguments: tuple[str, ...],
    *,
    old_filtergraph: str,
    new_filtergraph: str,
) -> tuple[str, ...]:
    values = list(arguments)
    try:
        index = values.index("-filter_complex")
    except ValueError as exc:
        raise FFmpegCompileError("base manifest has no filter_complex argument") from exc
    if index + 1 >= len(values) or values[index + 1] != old_filtergraph:
        raise FFmpegCompileError(
            "base manifest filter_complex argument does not match its filtergraph"
        )
    values[index + 1] = new_filtergraph
    return tuple(values)


def compile_ffmpeg_command(
    plan: RenderPlan,
    asset_paths: AssetPathSource,
    capabilities: FFmpegCapabilities,
    output_path: str | Path,
    *,
    prefer_nvenc: bool = True,
) -> RenderCommandManifest:
    """Compile PR23 presentation after existing PR13 motion and PR14 audio semantics."""

    has_focus = any(scene.motion_type == _FOCUS_MOTION_TYPE for scene in plan.scenes)
    has_ambience_duck = any(
        track.track_type == "ambience" and track.properties.get("duck_db") is not None
        for track in plan.audio_tracks
    )
    if not has_focus and not has_ambience_duck:
        return _compile_audio_ffmpeg_command(
            plan,
            asset_paths,
            capabilities,
            output_path,
            prefer_nvenc=prefer_nvenc,
        )

    base_plan = plan.validated_copy(
        update={"scenes": tuple(_clear_focus_motion(scene) for scene in plan.scenes)}
    )
    base_manifest = _compile_audio_ffmpeg_command(
        base_plan,
        asset_paths,
        capabilities,
        output_path,
        prefer_nvenc=prefer_nvenc,
    )
    filtergraph, focus_count, ambience_duck_count = _rewrite_presentation(
        plan,
        base_manifest,
        capabilities,
    )
    metadata = base_manifest.model_dump(mode="json")["metadata"]
    metadata.update(
        {
            "presentation_backend": "pr23_v1",
            "focus_zoom_scene_count": focus_count,
            "ambience_duck_track_count": ambience_duck_count,
        }
    )
    return base_manifest.validated_copy(
        update={
            "render_plan_digest": render_plan_digest(plan),
            "filtergraph": filtergraph,
            "arguments": _replace_filter_complex(
                base_manifest.arguments,
                old_filtergraph=base_manifest.filtergraph,
                new_filtergraph=filtergraph,
            ),
            "metadata": metadata,
        }
    )


__all__ = [
    "AssetPathSource",
    "FFmpegCompileError",
    "MissingRenderAssetError",
    "RuntimeStorageResolver",
    "UnsupportedRenderFeatureError",
    "compile_ffmpeg_command",
]
