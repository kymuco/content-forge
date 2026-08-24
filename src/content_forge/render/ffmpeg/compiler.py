"""Deterministic FFmpeg command/filtergraph compiler for RenderPlan."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from content_forge.core import FitMode, MediaType
from content_forge.storage import RuntimePaths
from content_forge.timeline import PlannedAsset, RenderPlan, render_plan_digest

from .capabilities import require_filters, select_h264_encoder
from .geometry import resolve_pixel_rect
from .models import (
    FFmpegCapabilities,
    RenderCommandManifest,
    RenderInput,
    canonical_output_path,
)

_SAFE_STYLE_VALUE = re.compile(r"^[A-Za-z0-9#@._+-]+$")
_XFADE_TRANSITIONS = {
    "crossfade": "fade",
    "fade": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "wipeup": "wipeup",
    "wipedown": "wipedown",
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
}


class FFmpegCompileError(ValueError):
    pass


class MissingRenderAssetError(FFmpegCompileError):
    pass


class UnsupportedRenderFeatureError(FFmpegCompileError):
    pass


class PlannedAssetPathResolver(Protocol):
    def resolve(self, asset: PlannedAsset) -> str | Path: ...


AssetPathSource = Mapping[str, str | Path] | PlannedAssetPathResolver


@dataclass(frozen=True, slots=True)
class RuntimeStorageResolver:
    """Resolve planned assets from PR3 canonical runtime storage keys."""

    paths: RuntimePaths

    def resolve(self, asset: PlannedAsset) -> Path:
        expected_key = self.paths.storage_key_for_sha256(asset.sha256)
        if asset.storage_key != expected_key:
            raise MissingRenderAssetError(
                f"planned asset storage key is not canonical: {asset.asset_id}"
            )
        path = self.paths.root / expected_key
        if not path.is_file():
            raise MissingRenderAssetError(f"planned asset blob is missing: {path}")
        return path


def _number(value: float) -> str:
    text = f"{float(value):.9f}".rstrip("0").rstrip(".")
    return text or "0"


def _style_value(value: object, *, name: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not _SAFE_STYLE_VALUE.fullmatch(value):
        raise FFmpegCompileError(f"invalid drawtext {name}")
    return value


def _integer_property(value: object, *, name: str, default: int, minimum: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FFmpegCompileError(f"invalid integer overlay property {name}")
    return value


def _quote_drawtext_text(value: str) -> str:
    """Quote text across FFmpeg filtergraph and drawtext option parsing.

    FFmpeg single-quoted values cannot contain an escaped apostrophe inside the quote.
    Close the quote, emit an escaped apostrophe outside it, then reopen the quote. The
    colon/backslash/newline escaping remains for drawtext's secondary option parser.
    """

    escaped = (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("\r", "")
        .replace("\n", "\\n")
    )
    return "'" + escaped.replace("'", "'\\''") + "'"


def _resolve_asset_path(source: AssetPathSource, asset: PlannedAsset) -> Path:
    if isinstance(source, Mapping):
        value = source.get(asset.asset_id)
        if value is None:
            raise MissingRenderAssetError(
                f"no local path supplied for planned asset: {asset.asset_id}"
            )
    else:
        value = source.resolve(asset)
    path = Path(value).expanduser()
    if not path.is_file():
        raise MissingRenderAssetError(f"render asset path is not a file: {path}")
    return path.resolve()


def _fit_filter(
    fit_mode: FitMode,
    *,
    width: int,
    height: int,
    focus_x: float,
    focus_y: float,
) -> str:
    if fit_mode is FitMode.STRETCH:
        return f"scale={width}:{height}"
    if fit_mode is FitMode.COVER:
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-ow)*{_number(focus_x)}:(ih-oh)*{_number(focus_y)}"
        )
    if fit_mode is FitMode.CONTAIN:
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    if fit_mode is FitMode.BLUR_BACKGROUND:
        raise UnsupportedRenderFeatureError(
            "blur_background requires the split/composite scene path"
        )
    raise UnsupportedRenderFeatureError(f"unsupported fit mode: {fit_mode}")


def _crop_prefix(scene: object) -> str:
    crop = getattr(scene, "crop", None)
    if crop is None:
        return ""
    return (
        "crop="
        f"iw*{_number(crop.width)}:ih*{_number(crop.height)}:"
        f"iw*{_number(crop.x)}:ih*{_number(crop.y)},"
    )


def compile_ffmpeg_command(
    plan: RenderPlan,
    asset_paths: AssetPathSource,
    capabilities: FFmpegCapabilities,
    output_path: str | Path,
    *,
    prefer_nvenc: bool = True,
) -> RenderCommandManifest:
    """Compile one validated RenderPlan into an argv-only FFmpeg command manifest."""

    profile = plan.output_profile
    if profile.container != "mp4":
        raise UnsupportedRenderFeatureError(
            f"PR5 FFmpeg backend currently supports mp4 only, got {profile.container}"
        )
    if profile.video_codec != "h264":
        raise UnsupportedRenderFeatureError(
            f"PR5 FFmpeg backend currently supports h264 video only, got {profile.video_codec}"
        )
    if profile.audio_codec not in {None, "aac"}:
        raise UnsupportedRenderFeatureError(
            f"PR5 FFmpeg backend currently supports AAC audio only, got {profile.audio_codec}"
        )
    if profile.width % 2 or profile.height % 2:
        raise FFmpegCompileError("H.264 output dimensions must be even")

    encoder = select_h264_encoder(capabilities, prefer_nvenc=prefer_nvenc)
    asset_by_id = {asset.asset_id: asset for asset in plan.assets}
    resolved_paths = {
        asset.asset_id: _resolve_asset_path(asset_paths, asset) for asset in plan.assets
    }

    required = {"color", "format", "fps", "overlay", "scale", "setpts", "trim"}
    filter_parts: list[str] = []
    input_arguments: list[str] = []
    render_inputs: list[RenderInput] = []

    def add_input(
        asset_id: str,
        *,
        role: str,
        duration: float,
        seek: float = 0.0,
        loop: bool = False,
        image: bool = False,
    ) -> int:
        asset = asset_by_id.get(asset_id)
        if asset is None:
            raise MissingRenderAssetError(f"unknown planned asset: {asset_id}")
        path = resolved_paths[asset_id]
        index = len(render_inputs)
        if loop and not image:
            input_arguments.extend(("-stream_loop", "-1"))
        if image:
            input_arguments.extend(("-loop", "1", "-framerate", _number(profile.fps)))
        if seek > 0.0:
            input_arguments.extend(("-ss", _number(seek)))
        input_arguments.extend(("-t", _number(duration), "-i", str(path)))
        render_inputs.append(
            RenderInput(
                input_index=index,
                asset_id=asset_id,
                path=str(path),
                media_type=asset.media_type,
                role=role,
                loop=loop or image,
                seek_seconds=seek,
                duration_seconds=duration,
            )
        )
        return index

    # Compile each scene into a full-canvas video stream starting at PTS zero.
    for scene_index, scene in enumerate(plan.scenes):
        if scene.motion_type is not None:
            raise UnsupportedRenderFeatureError(
                f"PR5 backend does not yet render motion type {scene.motion_type!r}"
            )
        duration = _number(scene.duration_seconds)
        canvas = f"scene_base_{scene_index}"
        output = f"scene_{scene_index}"
        filter_parts.append(
            f"color=c=black:s={profile.width}x{profile.height}:r={_number(profile.fps)}:d={duration}[{canvas}]"
        )
        if scene.media_asset_id is None:
            filter_parts.append(
                f"[{canvas}]format=yuv420p,setpts=PTS-STARTPTS[{output}]"
            )
            continue

        asset = asset_by_id[scene.media_asset_id]
        requested_duration = (
            scene.trim_duration_seconds
            if scene.trim_duration_seconds is not None
            else scene.duration_seconds
        )
        input_index = add_input(
            scene.media_asset_id,
            role=f"scene:{scene.scene_id}",
            duration=requested_duration,
            seek=scene.trim_start_seconds,
            image=asset.media_type is MediaType.IMAGE,
        )
        placement = resolve_pixel_rect(scene.placement, profile)
        focus_x = 0.5 if scene.focus is None else scene.focus.x
        focus_y = 0.5 if scene.focus is None else scene.focus.y
        crop_prefix = _crop_prefix(scene)
        source_prefix = (
            f"[{input_index}:v]{crop_prefix}trim=duration={duration},"
            f"setpts=PTS-STARTPTS,fps={_number(profile.fps)},"
        )

        fitted = f"scene_fit_{scene_index}"
        if scene.fit_mode is FitMode.BLUR_BACKGROUND:
            required.update({"boxblur", "crop", "split"})
            background_source = f"scene_bg_src_{scene_index}"
            foreground_source = f"scene_fg_src_{scene_index}"
            background = f"scene_bg_{scene_index}"
            foreground = f"scene_fg_{scene_index}"
            filter_parts.append(
                source_prefix
                + f"split=2[{background_source}][{foreground_source}]"
            )
            filter_parts.append(
                f"[{background_source}]scale={placement.width}:{placement.height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={placement.width}:{placement.height},boxblur=20:2[{background}]"
            )
            filter_parts.append(
                f"[{foreground_source}]scale={placement.width}:{placement.height}:"
                f"force_original_aspect_ratio=decrease[{foreground}]"
            )
            filter_parts.append(
                f"[{background}][{foreground}]overlay=(W-w)/2:(H-h)/2:shortest=1[{fitted}]"
            )
        else:
            if scene.fit_mode is FitMode.COVER:
                required.add("crop")
            if scene.fit_mode is FitMode.CONTAIN:
                required.add("pad")
            fit = _fit_filter(
                scene.fit_mode,
                width=placement.width,
                height=placement.height,
                focus_x=focus_x,
                focus_y=focus_y,
            )
            filter_parts.append(source_prefix + fit + f"[{fitted}]")

        filter_parts.append(
            f"[{canvas}][{fitted}]overlay={placement.x}:{placement.y}:shortest=1:format=auto,"
            f"format=yuv420p,setpts=PTS-STARTPTS[{output}]"
        )

    # Join scene streams. Cut uses concat; non-cut transitions use xfade.
    current_label = "scene_0"
    for index in range(1, len(plan.scenes)):
        transition = plan.scenes[index].transition_in
        next_label = f"scene_{index}"
        joined = f"timeline_{index}"
        if transition.transition_type == "cut":
            required.add("concat")
            filter_parts.append(
                f"[{current_label}][{next_label}]concat=n=2:v=1:a=0[{joined}]"
            )
        else:
            required.add("xfade")
            transition_name = _XFADE_TRANSITIONS.get(transition.transition_type)
            if transition_name is None:
                raise UnsupportedRenderFeatureError(
                    f"unsupported FFmpeg xfade transition: {transition.transition_type}"
                )
            filter_parts.append(
                f"[{current_label}][{next_label}]xfade=transition={transition_name}:"
                f"duration={_number(transition.duration_seconds)}:"
                f"offset={_number(plan.scenes[index].start_seconds)}[{joined}]"
            )
        current_label = joined

    filter_parts.append(
        f"[{current_label}]trim=duration={_number(plan.total_duration_seconds)},"
        "setpts=PTS-STARTPTS[video_timeline]"
    )
    current_video = "video_timeline"

    # Apply resolved visual/text overlays in z-order; enable expressions preserve timing.
    for overlay_index, overlay in enumerate(
        sorted(plan.overlays, key=lambda item: (item.z_index, item.overlay_id))
    ):
        placement = resolve_pixel_rect(overlay.placement, profile)
        enable = (
            f"between(t,{_number(overlay.start_seconds)},{_number(overlay.end_seconds)})"
        )
        if overlay.asset_id is not None:
            asset = asset_by_id[overlay.asset_id]
            input_index = add_input(
                overlay.asset_id,
                role=f"overlay:{overlay.overlay_id}",
                duration=overlay.duration_seconds,
                image=asset.media_type is MediaType.IMAGE,
            )
            overlay_stream = f"overlay_asset_{overlay_index}"
            filter_parts.append(
                f"[{input_index}:v]trim=duration={_number(overlay.duration_seconds)},"
                "setpts=PTS-STARTPTS+"
                f"{_number(overlay.start_seconds)}/TB,"
                f"scale={placement.width}:{placement.height},format=rgba[{overlay_stream}]"
            )
            output = f"video_overlay_asset_{overlay_index}"
            filter_parts.append(
                f"[{current_video}][{overlay_stream}]overlay={placement.x}:{placement.y}:"
                f"enable='{enable}':eof_action=pass:format=auto[{output}]"
            )
            current_video = output

        if overlay.text is not None:
            required.add("drawtext")
            font_size = _integer_property(
                overlay.properties.get("font_size"),
                name="font_size",
                default=max(12, placement.height // 3),
                minimum=1,
            )
            border_width = _integer_property(
                overlay.properties.get("border_width"),
                name="border_width",
                default=0,
            )
            font_value = overlay.properties.get("font")
            font_option = ""
            if font_value is not None:
                font = _style_value(
                    font_value,
                    name="font",
                    default="sans-serif",
                )
                font_option = f":font={font}"
            font_color = _style_value(
                overlay.properties.get("font_color"),
                name="font_color",
                default="white",
            )
            border_color = _style_value(
                overlay.properties.get("border_color"),
                name="border_color",
                default="black",
            )
            box_color = _style_value(
                overlay.properties.get("box_color"),
                name="box_color",
                default="black@0.45",
            )
            box = overlay.properties.get("box", False)
            if not isinstance(box, bool):
                raise FFmpegCompileError("drawtext box property must be boolean")
            quoted_text = _quote_drawtext_text(overlay.text)
            output = f"video_text_{overlay_index}"
            draw = (
                f"drawtext=text={quoted_text}:expansion=none:x={placement.x}:y={placement.y}:"
                f"fontsize={font_size}{font_option}:fontcolor={font_color}:borderw={border_width}:"
                f"bordercolor={border_color}:box={1 if box else 0}:boxcolor={box_color}:"
                f"enable='{enable}'"
            )
            filter_parts.append(f"[{current_video}]{draw}[{output}]")
            current_video = output

        if overlay.asset_id is None and overlay.text is None:
            raise UnsupportedRenderFeatureError(
                f"overlay has no PR5-renderable asset or text: {overlay.overlay_id}"
            )

    filter_parts.append(f"[{current_video}]format=yuv420p[vout]")

    audio_output: str | None = None
    if profile.audio_codec is not None:
        required.update({"aformat", "amix", "anullsrc", "asetpts", "atrim", "volume"})
        total = _number(plan.total_duration_seconds)
        filter_parts.append(
            f"anullsrc=r=48000:cl=stereo,atrim=duration={total},asetpts=PTS-STARTPTS[audio_silence]"
        )
        audio_labels = ["audio_silence"]
        for audio_index, track in enumerate(plan.audio_tracks):
            if track.asset_id is None:
                raise UnsupportedRenderFeatureError(
                    f"assetless audio is not renderable in PR5: {track.audio_track_id}"
                )
            asset = asset_by_id[track.asset_id]
            input_index = add_input(
                track.asset_id,
                role=f"audio:{track.audio_track_id}",
                duration=track.duration_seconds,
                seek=track.source_start_seconds,
                loop=track.loop,
                image=False,
            )
            label = f"audio_{audio_index}"
            filter_parts.append(
                f"[{input_index}:a]atrim=duration={_number(track.duration_seconds)},"
                "asetpts=PTS-STARTPTS+"
                f"{_number(track.start_seconds)}/TB,"
                f"volume={_number(track.gain_db)}dB,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
                f"[{label}]"
            )
            audio_labels.append(label)
        if len(audio_labels) == 1:
            audio_output = audio_labels[0]
        else:
            audio_output = "aout"
            joined = "".join(f"[{label}]" for label in audio_labels)
            filter_parts.append(
                joined
                + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0,"
                + f"atrim=duration={total}[{audio_output}]"
            )

    require_filters(capabilities, required)

    destination = canonical_output_path(output_path)
    arguments: list[str] = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *input_arguments,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[vout]",
    ]
    if audio_output is not None:
        arguments.extend(("-map", f"[{audio_output}]"))

    arguments.extend(("-c:v", encoder))
    if profile.video_bitrate_kbps is not None:
        arguments.extend(("-b:v", f"{profile.video_bitrate_kbps}k"))
    elif encoder == "h264_nvenc":
        arguments.extend(("-preset", "p5", "-cq", "19"))
    else:
        arguments.extend(("-preset", "medium", "-crf", "18"))
    arguments.extend(("-pix_fmt", "yuv420p", "-r", _number(profile.fps)))

    if audio_output is not None:
        arguments.extend(("-c:a", "aac"))
        if profile.audio_bitrate_kbps is not None:
            arguments.extend(("-b:a", f"{profile.audio_bitrate_kbps}k"))
        else:
            arguments.extend(("-b:a", "192k"))
    else:
        arguments.append("-an")

    arguments.extend(
        (
            "-t",
            _number(plan.total_duration_seconds),
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            destination,
        )
    )

    return RenderCommandManifest(
        render_plan_digest=render_plan_digest(plan),
        ffmpeg_path=capabilities.ffmpeg_path,
        output_path=destination,
        video_encoder=encoder,
        filtergraph=";".join(filter_parts),
        arguments=tuple(arguments),
        inputs=tuple(render_inputs),
        metadata={
            "output_profile_id": profile.profile_id,
            "width": profile.width,
            "height": profile.height,
            "fps": profile.fps,
            "nvenc_requested": prefer_nvenc,
            "nvenc_usable": capabilities.h264_nvenc_usable,
        },
    )
