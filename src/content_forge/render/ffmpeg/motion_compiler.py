"""PR13 motion-aware wrapper around the stable PR5 FFmpeg compiler.

Ordinary scene/overlay/audio/transition behavior is delegated unchanged. PR13 rewrites only
the deterministic fitted-scene fragments needed for supported generic motion primitives.
"""

from __future__ import annotations

from pathlib import Path

from content_forge.core import FitMode, MediaType, NormalizedRect
from content_forge.timeline import PlannedAsset, PlannedScene, RenderPlan, render_plan_digest

from .capabilities import require_filters
from .compiler import (
    AssetPathSource,
    FFmpegCompileError,
    MissingRenderAssetError,
    RuntimeStorageResolver,
    UnsupportedRenderFeatureError,
    compile_ffmpeg_command as _compile_base_ffmpeg_command,
)
from .geometry import resolve_pixel_rect
from .models import FFmpegCapabilities, RenderCommandManifest

_CROP_MOTION_TYPES = frozenset({"slow_zoom", "pan", "crop_reveal"})
_SUPPORTED_MOTION_TYPES = _CROP_MOTION_TYPES | {"blur_reveal"}


def _number(value: float) -> str:
    text = f"{float(value):.9f}".rstrip("0").rstrip(".")
    return text or "0"


def _clear_motion(scene: PlannedScene) -> PlannedScene:
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
            f"motion scene has no media asset: {scene.scene_id}"
        )
    for asset in plan.assets:
        if asset.asset_id == scene.media_asset_id:
            return asset
    raise MissingRenderAssetError(
        f"motion scene references missing planned asset: {scene.media_asset_id}"
    )


def _input_index_for_scene(manifest: RenderCommandManifest, scene_id: str) -> int:
    expected = f"scene:{scene_id}"
    candidates = [item.input_index for item in manifest.inputs if item.role == expected]
    if len(candidates) != 1:
        raise FFmpegCompileError(
            f"cannot identify unique FFmpeg input for motion scene: {scene_id}"
        )
    return candidates[0]


def _fit_part_index(parts: list[str], scene_index: int) -> int:
    suffix = f"[scene_fit_{scene_index}]"
    candidates = [index for index, part in enumerate(parts) if part.endswith(suffix)]
    if len(candidates) != 1:
        raise FFmpegCompileError(
            f"cannot identify unique fitted stream for motion scene {scene_index}"
        )
    return candidates[0]


def _validate_motion_rect(
    rect: NormalizedRect,
    *,
    asset: PlannedAsset,
    target_width: int,
    target_height: int,
) -> None:
    if asset.width is None or asset.height is None:
        raise UnsupportedRenderFeatureError(
            "crop-window motion requires source width/height metadata"
        )
    source_crop_aspect = (asset.width * rect.width) / (asset.height * rect.height)
    target_aspect = target_width / target_height
    tolerance = max(1e-6, target_aspect * 1e-5)
    if abs(source_crop_aspect - target_aspect) > tolerance:
        raise UnsupportedRenderFeatureError(
            "motion crop rectangle aspect does not match the resolved scene placement"
        )


def _interpolated(start: float, end: float, progress: str) -> str:
    return f"({_number(start)}+({_number(end - start)})*{progress})"


def _dynamic_crop_part(
    plan: RenderPlan,
    scene: PlannedScene,
    *,
    scene_index: int,
    input_index: int,
    asset: PlannedAsset,
) -> str:
    """Compile a normalized source-window path without changing source aspect ratio.

    FFmpeg zoompan always crops a window with the input frame's aspect ratio before
    scaling it to the requested output size. That is not equivalent to our canonical
    normalized source rectangles when source and output aspects differ. Instead, scale
    the complete source uniformly on every frame so the requested semantic window covers
    the target rectangle, then crop the fixed target pixel rectangle at the interpolated
    normalized source origin. The scale filter keeps the original aspect ratio; crop owns
    only selection, so no hidden source stretching is introduced.
    """

    if asset.media_type is not MediaType.IMAGE:
        raise UnsupportedRenderFeatureError(
            f"{scene.motion_type} currently supports image scenes only"
        )
    if scene.fit_mode is not FitMode.COVER:
        raise UnsupportedRenderFeatureError(
            f"{scene.motion_type} requires cover fit semantics"
        )
    if scene.crop is not None:
        raise UnsupportedRenderFeatureError(
            f"{scene.motion_type} cannot be combined with an additional canonical crop"
        )
    start = scene.motion_start_rect
    end = scene.motion_end_rect
    if start is None or end is None:
        raise UnsupportedRenderFeatureError(
            f"{scene.motion_type} requires start and end crop rectangles"
        )

    placement = resolve_pixel_rect(scene.placement, plan.output_profile)
    _validate_motion_rect(
        start,
        asset=asset,
        target_width=placement.width,
        target_height=placement.height,
    )
    _validate_motion_rect(
        end,
        asset=asset,
        target_width=placement.width,
        target_height=placement.height,
    )

    total_frames = max(2, int(round(scene.duration_seconds * plan.output_profile.fps)))
    denominator = total_frames - 1
    progress = f"min(n/{denominator},1)"
    width = _interpolated(start.width, end.width, progress)
    height = _interpolated(start.height, end.height, progress)
    x = _interpolated(start.x, end.x, progress)
    y = _interpolated(start.y, end.y, progress)
    duration = _number(scene.duration_seconds)
    fps = _number(plan.output_profile.fps)

    # Both scale dimensions express the semantic source window independently. The
    # force_original_aspect_ratio=increase policy then chooses one uniform source scale,
    # so raster rounding can only over-cover the requested crop rather than distort it.
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


def _blur_reveal_parts(
    scene: PlannedScene,
    original_fit_part: str,
    *,
    scene_index: int,
) -> tuple[str, ...]:
    value = scene.motion_properties.get("reveal_duration_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnsupportedRenderFeatureError(
            "blur_reveal requires numeric reveal_duration_seconds"
        )
    reveal = float(value)
    if reveal <= 0.0 or reveal - scene.duration_seconds > 1e-9:
        raise UnsupportedRenderFeatureError(
            "blur_reveal duration must be positive and not exceed scene duration"
        )

    target = f"[scene_fit_{scene_index}]"
    if not original_fit_part.endswith(target):
        raise FFmpegCompileError("unexpected fitted-stream fragment for blur_reveal")
    base = f"scene_motion_base_{scene_index}"
    blur_source = f"scene_blur_source_{scene_index}"
    sharp_source = f"scene_sharp_source_{scene_index}"
    blurred = f"scene_blurred_{scene_index}"
    rewritten = original_fit_part[: -len(target)] + f"[{base}]"
    reveal_number = _number(reveal)
    expression = f"A*(1-min(T/{reveal_number},1))+B*min(T/{reveal_number},1)"
    return (
        rewritten,
        f"[{base}]split=2[{blur_source}][{sharp_source}]",
        f"[{blur_source}]boxblur=20:2[{blurred}]",
        f"[{blurred}][{sharp_source}]blend=all_expr='{expression}'{target}",
    )


def _rewrite_filtergraph(
    plan: RenderPlan,
    manifest: RenderCommandManifest,
    capabilities: FFmpegCapabilities,
) -> str:
    parts = manifest.filtergraph.split(";")
    extra_filters: set[str] = set()

    for scene_index, scene in enumerate(plan.scenes):
        motion_type = scene.motion_type
        if motion_type is None:
            continue
        if motion_type not in _SUPPORTED_MOTION_TYPES:
            raise UnsupportedRenderFeatureError(
                f"PR13 backend does not render motion type {motion_type!r}"
            )
        asset = _asset_for_scene(plan, scene)
        fit_index = _fit_part_index(parts, scene_index)
        if motion_type in _CROP_MOTION_TYPES:
            input_index = _input_index_for_scene(manifest, scene.scene_id)
            parts[fit_index] = _dynamic_crop_part(
                plan,
                scene,
                scene_index=scene_index,
                input_index=input_index,
                asset=asset,
            )
        elif motion_type == "blur_reveal":
            parts[fit_index : fit_index + 1] = _blur_reveal_parts(
                scene, parts[fit_index], scene_index=scene_index
            )
            extra_filters.update({"blend", "boxblur", "split"})

    if extra_filters:
        require_filters(capabilities, extra_filters)
    return ";".join(parts)


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
    """Compile supported PR13 motion while delegating all other semantics to PR5."""

    if not any(scene.motion_type is not None for scene in plan.scenes):
        return _compile_base_ffmpeg_command(
            plan,
            asset_paths,
            capabilities,
            output_path,
            prefer_nvenc=prefer_nvenc,
        )
    for scene in plan.scenes:
        if scene.motion_type is not None and scene.motion_type not in _SUPPORTED_MOTION_TYPES:
            raise UnsupportedRenderFeatureError(
                f"PR13 backend does not render motion type {scene.motion_type!r}"
            )

    base_plan = plan.validated_copy(
        update={"scenes": tuple(_clear_motion(scene) for scene in plan.scenes)}
    )
    base_manifest = _compile_base_ffmpeg_command(
        base_plan,
        asset_paths,
        capabilities,
        output_path,
        prefer_nvenc=prefer_nvenc,
    )
    filtergraph = _rewrite_filtergraph(plan, base_manifest, capabilities)
    metadata = base_manifest.model_dump(mode="json")["metadata"]
    metadata.update(
        {
            "motion_backend": "pr13_v1",
            "motion_geometry": "aspect_preserving_source_rect_v1",
            "motion_scene_count": sum(
                1 for scene in plan.scenes if scene.motion_type is not None
            ),
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
