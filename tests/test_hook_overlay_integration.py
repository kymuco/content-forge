from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    Project,
    Scene,
    TemplateRef,
    Variant,
    new_entity_id,
)
from content_forge.profiles import shorts_preview_profile
from content_forge.render.ffmpeg import (
    FFmpegBackend,
    FFmpegCapabilityError,
    probe_ffmpeg_runtime,
    probe_media,
)
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    compile_hook_overlay,
)


def test_hook_overlay_template_renders_real_vertical_mp4(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg runtime is not installed")

    capabilities = probe_ffmpeg_runtime(test_nvenc=False)
    if not capabilities.has_libx264:
        pytest.skip("integration fixture requires libx264 CPU fallback")

    source = tmp_path / "source.ppm"
    pixels: list[str] = []
    for y in range(48):
        for x in range(32):
            pixels.append("245 80 70" if (x + y) % 2 else "40 100 230")
    source.write_text(
        "P3\n32 48\n255\n" + "\n".join(pixels) + "\n",
        encoding="ascii",
    )

    asset = Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="e" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
        size_bytes=source.stat().st_size,
        width=32,
        height=48,
        has_audio=False,
    )
    project = Project(
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        variants=(
            Variant(
                language="en",
                hook="Don't blink: this tiny detail changes everything!",
            ),
        ),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.45,
                media=AssetRef(asset_id=asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
    )

    plan = compile_hook_overlay(project, {asset.asset_id: asset})
    assert plan.overlays[0].text is not None
    assert plan.output_profile.width == 540
    assert plan.output_profile.height == 960

    destination = tmp_path / "hook-overlay.mp4"
    backend = FFmpegBackend(
        capabilities,
        {asset.asset_id: source},
        prefer_nvenc=False,
    )
    try:
        result = backend.render(plan, destination, timeout=20)
    except FFmpegCapabilityError as exc:
        pytest.skip(str(exc))

    assert result.bytes_written > 0
    probe = probe_media(destination, ffprobe_path=capabilities.ffprobe_path)
    assert probe.has_video is True
    assert probe.width == 540
    assert probe.height == 960
    assert probe.duration_seconds is not None
    assert 0.3 <= probe.duration_seconds <= 0.8
