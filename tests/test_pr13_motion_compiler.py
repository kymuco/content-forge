from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.core import (
    EntityKind,
    FitMode,
    MediaType,
    NormalizedRect,
    OutputProfile,
    new_entity_id,
)
from content_forge.render.ffmpeg import (
    FFmpegCapabilities,
    UnsupportedRenderFeatureError,
    compile_ffmpeg_command,
)
from content_forge.timeline import PlannedAsset, PlannedScene, RenderPlan, render_plan_digest

FILTERS = (
    "blend", "boxblur", "color", "crop", "format", "fps", "overlay",
    "scale", "setpts", "split", "trim", "zoompan",
)


def _capabilities() -> FFmpegCapabilities:
    return FFmpegCapabilities(
        ffmpeg_path="/synthetic/ffmpeg",
        ffprobe_path="/synthetic/ffprobe",
        ffmpeg_version="ffmpeg version synthetic",
        ffprobe_version="ffprobe version synthetic",
        encoders=("libx264",),
        filters=FILTERS,
        h264_nvenc_usable=False,
    )


def _asset(*, media_type: MediaType = MediaType.IMAGE) -> PlannedAsset:
    return PlannedAsset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="a" * 64,
        media_type=media_type,
        mime_type="image/png" if media_type is MediaType.IMAGE else "video/mp4",
        width=1000,
        height=1500,
        duration_seconds=None if media_type is MediaType.IMAGE else 4.0,
        has_audio=False,
    )


def _profile() -> OutputProfile:
    return OutputProfile(
        profile_id="preview", width=540, height=960, fps=30, audio_codec=None
    )


def _plan(asset: PlannedAsset, *, motion_type: str) -> RenderPlan:
    start = NormalizedRect(x=0.078125, y=0.0, width=0.84375, height=1.0)
    end = NormalizedRect(
        x=0.109375, y=0.037037037, width=0.78125, height=0.925925926
    )
    properties = (
        {"reveal_duration_seconds": 1.0} if motion_type == "blur_reveal" else {}
    )
    scene = PlannedScene(
        scene_id=new_entity_id(EntityKind.SCENE),
        order=0,
        start_seconds=0.0,
        duration_seconds=1.0,
        end_seconds=1.0,
        media_asset_id=asset.asset_id,
        placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
        fit_mode=FitMode.COVER,
        motion_type=motion_type,
        motion_start_rect=None if motion_type == "blur_reveal" else start,
        motion_end_rect=None if motion_type == "blur_reveal" else end,
        motion_properties=properties,
    )
    return RenderPlan(
        project_id=new_entity_id(EntityKind.PROJECT),
        output_profile=_profile(),
        total_duration_seconds=1.0,
        scenes=(scene,),
        assets=(asset,),
    )


@pytest.mark.parametrize("motion_type", ["slow_zoom", "pan", "crop_reveal"])
def test_crop_window_motion_rewrites_scene_fit_fragment(
    tmp_path: Path, motion_type: str
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-placeholder")
    asset = _asset()
    plan = _plan(asset, motion_type=motion_type)
    manifest = compile_ffmpeg_command(
        plan,
        {asset.asset_id: source},
        _capabilities(),
        tmp_path / f"{motion_type}.mp4",
        prefer_nvenc=False,
    )
    assert "zoompan=" in manifest.filtergraph
    assert manifest.metadata["motion_backend"] == "pr13_v1"
    assert manifest.metadata["motion_scene_count"] == 1
    assert manifest.render_plan_digest == render_plan_digest(plan)
    index = manifest.arguments.index("-filter_complex")
    assert manifest.arguments[index + 1] == manifest.filtergraph


def test_blur_reveal_reuses_base_fit_then_blends_streams(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-placeholder")
    asset = _asset()
    plan = _plan(asset, motion_type="blur_reveal")
    manifest = compile_ffmpeg_command(
        plan,
        {asset.asset_id: source},
        _capabilities(),
        tmp_path / "blur.mp4",
        prefer_nvenc=False,
    )
    assert "split=2" in manifest.filtergraph
    assert "boxblur=20:2" in manifest.filtergraph
    assert "blend=all_expr=" in manifest.filtergraph
    assert manifest.render_plan_digest == render_plan_digest(plan)


def test_crop_window_motion_fails_closed_for_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-placeholder")
    asset = _asset(media_type=MediaType.VIDEO)
    with pytest.raises(UnsupportedRenderFeatureError, match="image scenes only"):
        compile_ffmpeg_command(
            _plan(asset, motion_type="slow_zoom"),
            {asset.asset_id: source},
            _capabilities(),
            tmp_path / "out.mp4",
            prefer_nvenc=False,
        )


def test_unknown_motion_still_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-placeholder")
    asset = _asset()
    with pytest.raises(UnsupportedRenderFeatureError, match="does not render motion"):
        compile_ffmpeg_command(
            _plan(asset, motion_type="ken_burns"),
            {asset.asset_id: source},
            _capabilities(),
            tmp_path / "out.mp4",
            prefer_nvenc=False,
        )
