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
    FFmpegCompileError,
    UnsupportedRenderFeatureError,
    command_manifest_digest,
    compile_ffmpeg_command,
)
from content_forge.timeline import (
    PlannedAsset,
    PlannedAudioTrack,
    PlannedOverlay,
    PlannedScene,
    RenderPlan,
)


BASE_FILTERS = (
    "aformat",
    "amix",
    "anullsrc",
    "asetpts",
    "atrim",
    "color",
    "concat",
    "crop",
    "drawtext",
    "format",
    "fps",
    "overlay",
    "pad",
    "scale",
    "setpts",
    "trim",
    "volume",
    "xfade",
)


def capabilities(*, nvenc: bool = False) -> FFmpegCapabilities:
    return FFmpegCapabilities(
        ffmpeg_path="/synthetic/ffmpeg",
        ffprobe_path="/synthetic/ffprobe",
        ffmpeg_version="ffmpeg version synthetic",
        ffprobe_version="ffprobe version synthetic",
        encoders=("h264_nvenc", "libx264"),
        filters=BASE_FILTERS,
        h264_nvenc_usable=nvenc,
    )


def image_plan(path: Path, *, with_text: bool = False) -> tuple[RenderPlan, dict[str, Path]]:
    asset_id = new_entity_id(EntityKind.ASSET)
    scene_id = new_entity_id(EntityKind.SCENE)
    asset = PlannedAsset(
        asset_id=asset_id,
        sha256="a" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        width=640,
        height=640,
        has_audio=False,
    )
    scene = PlannedScene(
        scene_id=scene_id,
        order=0,
        start_seconds=0,
        duration_seconds=2,
        end_seconds=2,
        media_asset_id=asset_id,
        placement=NormalizedRect(x=0, y=0, width=1, height=1),
        fit_mode=FitMode.COVER,
    )
    overlays = ()
    if with_text:
        overlays = (
            PlannedOverlay(
                overlay_id=new_entity_id(EntityKind.OVERLAY),
                component_type="text",
                start_seconds=0.25,
                duration_seconds=1.5,
                end_seconds=1.75,
                placement=NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.15),
                z_index=10,
                text="Hello: Content Forge",
                properties={"font_size": 42, "box": True},
            ),
        )
    plan = RenderPlan(
        project_id=new_entity_id(EntityKind.PROJECT),
        output_profile=OutputProfile(
            profile_id="short_vertical",
            width=1080,
            height=1920,
            fps=30,
            audio_codec=None,
        ),
        total_duration_seconds=2,
        scenes=(scene,),
        overlays=overlays,
        assets=(asset,),
    )
    return plan, {asset_id: path}


def test_command_manifest_is_deterministic_and_uses_cpu_fallback(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-placeholder")
    plan, paths = image_plan(source, with_text=True)
    output = tmp_path / "out.mp4"

    first = compile_ffmpeg_command(plan, paths, capabilities(), output)
    second = compile_ffmpeg_command(plan, paths, capabilities(), output)

    assert first == second
    assert command_manifest_digest(first) == command_manifest_digest(second)
    assert first.video_encoder == "libx264"
    assert first.arguments[-1] == str(output.resolve())
    assert "-loop" in first.arguments
    assert "drawtext=" in first.filtergraph
    assert "crop=1080:1920" in first.filtergraph
    assert first.inputs[0].role.startswith("scene:")


def test_command_manifest_selects_nvenc_only_when_runtime_probe_succeeded(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-placeholder")
    plan, paths = image_plan(source)

    manifest = compile_ffmpeg_command(
        plan,
        paths,
        capabilities(nvenc=True),
        tmp_path / "out.mp4",
    )

    assert manifest.video_encoder == "h264_nvenc"
    assert "p5" in manifest.arguments


def test_audio_track_compiles_with_silence_base_and_mix(tmp_path: Path) -> None:
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.wav"
    video_path.write_bytes(b"video-placeholder")
    audio_path.write_bytes(b"audio-placeholder")
    video_id = new_entity_id(EntityKind.ASSET)
    audio_id = new_entity_id(EntityKind.ASSET)
    scene = PlannedScene(
        scene_id=new_entity_id(EntityKind.SCENE),
        order=0,
        start_seconds=0,
        duration_seconds=3,
        end_seconds=3,
        media_asset_id=video_id,
        placement=NormalizedRect(x=0, y=0, width=1, height=1),
        fit_mode=FitMode.CONTAIN,
    )
    track = PlannedAudioTrack(
        audio_track_id=new_entity_id(EntityKind.AUDIO),
        track_type="music",
        start_seconds=0.5,
        duration_seconds=2,
        end_seconds=2.5,
        asset_id=audio_id,
        source_start_seconds=0.25,
        gain_db=-6,
    )
    plan = RenderPlan(
        project_id=new_entity_id(EntityKind.PROJECT),
        output_profile=OutputProfile(
            profile_id="preview",
            width=540,
            height=960,
            fps=30,
            audio_codec="aac",
        ),
        total_duration_seconds=3,
        scenes=(scene,),
        audio_tracks=(track,),
        assets=(
            PlannedAsset(
                asset_id=video_id,
                sha256="b" * 64,
                media_type=MediaType.VIDEO,
                mime_type="video/mp4",
                duration_seconds=3,
                has_audio=False,
            ),
            PlannedAsset(
                asset_id=audio_id,
                sha256="c" * 64,
                media_type=MediaType.AUDIO,
                mime_type="audio/wav",
                duration_seconds=10,
                has_audio=True,
            ),
        ),
    )

    manifest = compile_ffmpeg_command(
        plan,
        {video_id: video_path, audio_id: audio_path},
        capabilities(),
        tmp_path / "out.mp4",
    )

    assert "anullsrc=" in manifest.filtergraph
    assert "amix=inputs=2" in manifest.filtergraph
    assert "volume=-6dB" in manifest.filtergraph
    assert [item.role.split(":", 1)[0] for item in manifest.inputs] == ["scene", "audio"]


def test_odd_h264_canvas_fails_before_command_generation(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-placeholder")
    plan, paths = image_plan(source)
    payload = plan.model_dump(mode="python")
    payload["output_profile"] = OutputProfile(
        profile_id="odd",
        width=541,
        height=960,
        fps=30,
        audio_codec=None,
    )
    odd = RenderPlan.model_validate(payload)

    with pytest.raises(FFmpegCompileError, match="must be even"):
        compile_ffmpeg_command(odd, paths, capabilities(), tmp_path / "out.mp4")


def test_motion_fails_closed_until_backend_implements_it(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic-placeholder")
    plan, paths = image_plan(source)
    scene = plan.scenes[0].validated_copy(update={"motion_type": "ken_burns"})
    moving = plan.validated_copy(update={"scenes": (scene,)})

    with pytest.raises(UnsupportedRenderFeatureError, match="motion"):
        compile_ffmpeg_command(moving, paths, capabilities(), tmp_path / "out.mp4")
