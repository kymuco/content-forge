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
    INITIAL_TEMPLATE_VERSION,
    REACTION_BOTTOM_TEMPLATE_ID,
    SYNC_STACK_TEMPLATE_ID,
    compile_registered_template,
)


def _ppm(path: Path, *, left: tuple[int, int, int], right: tuple[int, int, int]) -> None:
    pixels: list[str] = []
    for y in range(48):
        for x in range(64):
            color = left if x < 32 else right
            pixels.append(f"{color[0]} {color[1]} {color[2]}")
    path.write_text("P3\n64 48\n255\n" + "\n".join(pixels) + "\n", encoding="ascii")


def _asset(path: Path, *, sha: str) -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256=sha * 64,
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


def _assert_render(plan, paths: dict[str, Path], destination: Path) -> None:
    capabilities = _capabilities()
    backend = FFmpegBackend(capabilities, paths, prefer_nvenc=False)
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
    assert 0.3 <= probe.duration_seconds <= 0.9


def test_sync_stack_renders_through_existing_ffmpeg_asset_overlay_path(tmp_path: Path) -> None:
    source = tmp_path / "source.ppm"
    _ppm(source, left=(230, 60, 70), right=(30, 100, 230))
    asset = _asset(source, sha="a")
    project = Project(
        content_kind="sync_meme",
        template=TemplateRef(
            template_id=SYNC_STACK_TEMPLATE_ID,
            version=INITIAL_TEMPLATE_VERSION,
        ),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.45,
                media=AssetRef(asset_id=asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
        metadata={"sync_stack.copies": 3},
    )

    plan = compile_registered_template(project, {asset.asset_id: asset})

    assert len(plan.scenes) == 1
    assert len([item for item in plan.overlays if item.asset_id == asset.asset_id]) == 2
    _assert_render(plan, {asset.asset_id: source}, tmp_path / "sync-stack.mp4")


def test_reaction_bottom_renders_distinct_reaction_through_existing_overlay_path(tmp_path: Path) -> None:
    source = tmp_path / "source.ppm"
    reaction_path = tmp_path / "reaction.ppm"
    _ppm(source, left=(20, 180, 120), right=(40, 80, 220))
    _ppm(reaction_path, left=(240, 210, 40), right=(220, 40, 160))
    main = _asset(source, sha="b")
    reaction = _asset(reaction_path, sha="c")
    project = Project(
        content_kind="reaction_story",
        template=TemplateRef(
            template_id=REACTION_BOTTOM_TEMPLATE_ID,
            version=INITIAL_TEMPLATE_VERSION,
        ),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.45,
                media=AssetRef(asset_id=main.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
        metadata={"reaction_bottom.reaction_asset_id": reaction.asset_id},
    )

    plan = compile_registered_template(
        project,
        {main.asset_id: main, reaction.asset_id: reaction},
    )

    assert plan.overlays[0].asset_id == reaction.asset_id
    _assert_render(
        plan,
        {main.asset_id: source, reaction.asset_id: reaction_path},
        tmp_path / "reaction-bottom.mp4",
    )
