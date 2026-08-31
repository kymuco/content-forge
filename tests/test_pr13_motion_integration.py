from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    FitMode,
    MediaType,
    NormalizedPoint,
    NormalizedRect,
    Project,
    Scene,
    new_entity_id,
)
from content_forge.profiles import shorts_preview_profile
from content_forge.render.ffmpeg import (
    FFmpegBackend,
    FFmpegCapabilityError,
    probe_ffmpeg_runtime,
    probe_media,
)
from content_forge.templates import blur_reveal_motion, ken_burns_motion
from content_forge.timeline import compile_timeline


def _ppm(path: Path) -> None:
    pixels: list[str] = []
    for y in range(48):
        for x in range(64):
            if x < 32 and y < 24:
                color = (230, 60, 70)
            elif x >= 32 and y < 24:
                color = (40, 190, 90)
            elif x < 32:
                color = (50, 90, 230)
            else:
                color = (235, 200, 45)
            pixels.append(f"{color[0]} {color[1]} {color[2]}")
    path.write_text("P3\n64 48\n255\n" + "\n".join(pixels) + "\n", encoding="ascii")


def _asset(path: Path) -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="d" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
        size_bytes=path.stat().st_size,
        width=64,
        height=48,
        has_audio=False,
    )


def _capabilities():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg runtime is not installed")
    capabilities = probe_ffmpeg_runtime(test_nvenc=False)
    if not capabilities.has_libx264:
        pytest.skip("integration fixture requires libx264 CPU fallback")
    return capabilities


def _render(project: Project, asset: Asset, source: Path, output: Path) -> None:
    profile = shorts_preview_profile()
    plan = compile_timeline(
        project,
        {asset.asset_id: asset},
        profile_id=profile.profile_id,
    )
    backend = FFmpegBackend(
        _capabilities(), {asset.asset_id: source}, prefer_nvenc=False
    )
    try:
        result = backend.render(plan, output, timeout=20)
    except FFmpegCapabilityError as exc:
        pytest.skip(str(exc))
    assert result.bytes_written > 0
    probe = probe_media(output, ffprobe_path=backend.capabilities.ffprobe_path)
    assert probe.has_video is True
    assert probe.width == 540
    assert probe.height == 960
    assert probe.duration_seconds is not None
    assert 0.35 <= probe.duration_seconds <= 0.8


def test_slow_zoom_component_renders_through_public_ffmpeg_backend(tmp_path: Path) -> None:
    source = tmp_path / "source.ppm"
    _ppm(source)
    asset = _asset(source)
    profile = shorts_preview_profile()
    placement = NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0)
    motion = ken_burns_motion(
        asset,
        profile,
        placement,
        focus=NormalizedPoint(x=0.58, y=0.45),
        end_zoom=1.12,
    )
    project = Project(
        content_kind="motion_fixture",
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.45,
                media=AssetRef(asset_id=asset.asset_id),
                placement=placement,
                fit_mode=FitMode.COVER,
                motion=motion,
            ),
        ),
        output_profiles=(profile,),
    )
    _render(project, asset, source, tmp_path / "slow-zoom.mp4")


def test_blur_reveal_component_renders_through_public_ffmpeg_backend(tmp_path: Path) -> None:
    source = tmp_path / "source.ppm"
    _ppm(source)
    asset = _asset(source)
    profile = shorts_preview_profile()
    project = Project(
        content_kind="blur_fixture",
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.45,
                media=AssetRef(asset_id=asset.asset_id),
                placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
                fit_mode=FitMode.COVER,
                motion=blur_reveal_motion(reveal_duration_seconds=0.45),
            ),
        ),
        output_profiles=(profile,),
    )
    _render(project, asset, source, tmp_path / "blur-reveal.mp4")
