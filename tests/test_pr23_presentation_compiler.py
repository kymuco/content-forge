from __future__ import annotations

from pathlib import Path

from content_forge.core import (
    EntityKind,
    FitMode,
    MediaType,
    NormalizedPoint,
    NormalizedRect,
    OutputProfile,
    new_entity_id,
)
from content_forge.render.ffmpeg import FFmpegCapabilities, compile_ffmpeg_command
from content_forge.timeline import (
    PlannedAsset,
    PlannedAudioTrack,
    PlannedScene,
    RenderPlan,
    render_plan_digest,
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


def _capabilities() -> FFmpegCapabilities:
    return FFmpegCapabilities(
        ffmpeg_path="/synthetic/ffmpeg",
        ffprobe_path="/synthetic/ffprobe",
        ffmpeg_version="ffmpeg version synthetic",
        ffprobe_version="ffprobe version synthetic",
        encoders=("libx264",),
        filters=BASE_FILTERS,
    )


def _plan(tmp_path: Path) -> tuple[RenderPlan, dict[str, Path]]:
    project_id = new_entity_id(EntityKind.PROJECT)
    scene_id = new_entity_id(EntityKind.SCENE)
    image_id = new_entity_id(EntityKind.ASSET)
    voice_id = new_entity_id(EntityKind.ASSET)
    ambience_id = new_entity_id(EntityKind.ASSET)
    image = PlannedAsset(
        asset_id=image_id,
        sha256="1" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        width=1200,
        height=1600,
        has_audio=False,
    )
    voice = PlannedAsset(
        asset_id=voice_id,
        sha256="2" * 64,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
        duration_seconds=1.0,
        has_audio=True,
    )
    ambience = PlannedAsset(
        asset_id=ambience_id,
        sha256="3" * 64,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
        duration_seconds=10.0,
        has_audio=True,
    )
    scene = PlannedScene(
        scene_id=scene_id,
        order=0,
        start_seconds=0.0,
        duration_seconds=1.3,
        end_seconds=1.3,
        media_asset_id=image_id,
        placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
        fit_mode=FitMode.COVER,
        motion_type="focus_zoom",
        motion_focus=NormalizedPoint(x=0.62, y=0.42),
        motion_properties={
            "pr23_owner": "pr23_camera_v1",
            "start_scale": 0.86,
            "end_scale": 0.74,
        },
    )
    voice_track = PlannedAudioTrack(
        audio_track_id=new_entity_id(EntityKind.AUDIO),
        track_type="voice",
        scope_scene_id=scene_id,
        start_seconds=0.0,
        duration_seconds=1.0,
        end_seconds=1.0,
        asset_id=voice_id,
        gain_db=0.0,
    )
    ambience_track = PlannedAudioTrack(
        audio_track_id=new_entity_id(EntityKind.AUDIO),
        track_type="ambience",
        scope_scene_id=scene_id,
        start_seconds=0.0,
        duration_seconds=1.3,
        end_seconds=1.3,
        asset_id=ambience_id,
        gain_db=-3.0,
        properties={"duck_db": -6.0, "pr23_owner": "pr23_voiced_mix_v1"},
    )
    plan = RenderPlan(
        project_id=project_id,
        output_profile=OutputProfile(
            profile_id="vertical",
            width=540,
            height=960,
            fps=30.0,
        ),
        total_duration_seconds=1.3,
        scenes=(scene,),
        audio_tracks=(voice_track, ambience_track),
        assets=(image, voice, ambience),
    )
    panel_path = tmp_path / "panel.png"
    voice_path = tmp_path / "voice.wav"
    ambience_path = tmp_path / "ambience.wav"
    panel_path.write_bytes(b"panel")
    voice_path.write_bytes(b"voice")
    ambience_path.write_bytes(b"ambience")
    paths = {
        image_id: panel_path,
        voice_id: voice_path,
        ambience_id: ambience_path,
    }
    return plan, paths


def test_pr23_compiler_reuses_shared_motion_audio_pipeline(tmp_path: Path) -> None:
    plan, paths = _plan(tmp_path)
    manifest = compile_ffmpeg_command(
        plan,
        paths,
        _capabilities(),
        tmp_path / "output.mp4",
        prefer_nvenc=False,
    )

    assert manifest.render_plan_digest == render_plan_digest(plan)
    assert manifest.metadata["presentation_backend"] == "pr23_v1"
    assert manifest.metadata["focus_zoom_scene_count"] == 1
    assert manifest.metadata["ambience_duck_track_count"] == 1
    assert "crop=540:960:" in manifest.filtergraph
    assert "min(max(0.62-" in manifest.filtergraph
    assert "volume=-6dB:enable='between(t,0,1)'" in manifest.filtergraph
    assert "[audio_0]" in manifest.filtergraph
    assert "[audio_1]" in manifest.filtergraph


def test_pr23_focus_zoom_remains_profile_dependent_at_compile_time(tmp_path: Path) -> None:
    plan, paths = _plan(tmp_path)
    vertical = compile_ffmpeg_command(
        plan,
        paths,
        _capabilities(),
        tmp_path / "vertical.mp4",
        prefer_nvenc=False,
    )
    landscape_plan = plan.validated_copy(
        update={
            "output_profile": OutputProfile(
                profile_id="landscape",
                width=1920,
                height=1080,
                fps=30.0,
            )
        }
    )
    landscape = compile_ffmpeg_command(
        landscape_plan,
        paths,
        _capabilities(),
        tmp_path / "landscape.mp4",
        prefer_nvenc=False,
    )

    assert "crop=540:960:" in vertical.filtergraph
    assert "crop=1920:1080:" in landscape.filtergraph
    assert vertical.filtergraph != landscape.filtergraph
    assert vertical.render_plan_digest == render_plan_digest(plan)
    assert landscape.render_plan_digest == render_plan_digest(landscape_plan)
