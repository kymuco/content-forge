from __future__ import annotations

import shutil
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
    FFmpegBackend,
    FFmpegCapabilityError,
    probe_ffmpeg_runtime,
    probe_media,
)
from content_forge.timeline import PlannedAsset, PlannedOverlay, PlannedScene, RenderPlan


def test_synthetic_image_render_reaches_real_mp4_exit_condition(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg runtime is not installed")

    capabilities = probe_ffmpeg_runtime(test_nvenc=False)
    if not capabilities.has_libx264:
        pytest.skip("integration fixture requires libx264 CPU fallback")

    source = tmp_path / "synthetic.ppm"
    pixels = []
    for y in range(48):
        for x in range(64):
            pixels.append("255 64 32" if (x + y) % 2 else "32 96 255")
    source.write_text(
        "P3\n64 48\n255\n" + "\n".join(pixels) + "\n",
        encoding="ascii",
    )

    asset_id = new_entity_id(EntityKind.ASSET)
    scene = PlannedScene(
        scene_id=new_entity_id(EntityKind.SCENE),
        order=0,
        start_seconds=0,
        duration_seconds=0.6,
        end_seconds=0.6,
        media_asset_id=asset_id,
        placement=NormalizedRect(x=0, y=0, width=1, height=1),
        fit_mode=FitMode.COVER,
    )
    overlay = PlannedOverlay(
        overlay_id=new_entity_id(EntityKind.OVERLAY),
        component_type="text",
        start_seconds=0.05,
        duration_seconds=0.5,
        end_seconds=0.55,
        placement=NormalizedRect(x=0.05, y=0.05, width=0.9, height=0.2),
        z_index=1,
        text="Don't: inject; [filters], ever",
        properties={"font_size": 14, "box": True},
    )
    plan = RenderPlan(
        project_id=new_entity_id(EntityKind.PROJECT),
        output_profile=OutputProfile(
            profile_id="synthetic_mp4",
            width=160,
            height=90,
            fps=30,
            audio_codec=None,
        ),
        total_duration_seconds=0.6,
        scenes=(scene,),
        overlays=(overlay,),
        assets=(
            PlannedAsset(
                asset_id=asset_id,
                sha256="d" * 64,
                media_type=MediaType.IMAGE,
                mime_type="image/x-portable-pixmap",
                width=64,
                height=48,
                has_audio=False,
            ),
        ),
    )
    destination = tmp_path / "render.mp4"
    backend = FFmpegBackend(
        capabilities,
        {asset_id: source},
        prefer_nvenc=False,
    )

    try:
        result = backend.render(plan, destination, timeout=20)
    except FFmpegCapabilityError as exc:
        pytest.skip(str(exc))

    assert result.bytes_written > 0
    assert destination.is_file()
    probe = probe_media(destination, ffprobe_path=capabilities.ffprobe_path)
    assert probe.has_video is True
    assert probe.width == 160
    assert probe.height == 90
    assert probe.duration_seconds is not None
    assert 0.45 <= probe.duration_seconds <= 0.9
