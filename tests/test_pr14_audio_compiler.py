from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.audio import LoudnessMeasurement, audio_intermediate_cache_key
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
from content_forge.render.ffmpeg.motion_compiler import (
    compile_ffmpeg_command as compile_pre_pr14_command,
)
from content_forge.timeline import PlannedAsset, PlannedAudioTrack, PlannedScene, RenderPlan

FILTERS = (
    "afade",
    "aformat",
    "alimiter",
    "amix",
    "anullsrc",
    "asetpts",
    "atrim",
    "color",
    "crop",
    "format",
    "fps",
    "loudnorm",
    "overlay",
    "scale",
    "setpts",
    "trim",
    "volume",
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


def _plan(
    image_id: str,
    music_id: str,
    original_id: str,
    *,
    normalize: bool = False,
    measurement: LoudnessMeasurement | None = None,
    audio_features: bool = True,
) -> RenderPlan:
    scene = PlannedScene(
        scene_id=new_entity_id(EntityKind.SCENE),
        order=0,
        start_seconds=0.0,
        duration_seconds=2.0,
        end_seconds=2.0,
        media_asset_id=image_id,
        placement=NormalizedRect(x=0, y=0, width=1, height=1),
        fit_mode=FitMode.COVER,
    )
    music_properties = (
        {"fade_in_seconds": 0.2, "fade_out_seconds": 0.25, "duck_db": -8.0}
        if audio_features
        else {}
    )
    music = PlannedAudioTrack(
        audio_track_id=new_entity_id(EntityKind.AUDIO),
        track_type="music",
        start_seconds=0.0,
        duration_seconds=2.0,
        end_seconds=2.0,
        asset_id=music_id,
        gain_db=-10.0,
        loop=True,
        properties=music_properties,
    )
    original = PlannedAudioTrack(
        audio_track_id=new_entity_id(EntityKind.AUDIO),
        track_type="original",
        start_seconds=0.5,
        duration_seconds=0.75,
        end_seconds=1.25,
        asset_id=original_id,
        gain_db=0.0,
    )
    profile_properties: dict[str, object] = {}
    if audio_features:
        mastering: dict[str, object] = {
            "normalize": normalize,
            "target_integrated_lufs": -14.0,
            "target_true_peak_dbfs": -1.0,
            "target_lra": 11.0,
            "limiter_dbfs": -1.0,
        }
        if measurement is not None:
            mastering["measurement"] = measurement.model_dump(mode="json")
        profile_properties["audio_mastering"] = mastering
        profile_properties["audio_policy"] = {
            "policy_id": "fixture",
            "version": "1.0",
        }
    return RenderPlan(
        project_id=new_entity_id(EntityKind.PROJECT),
        output_profile=OutputProfile(
            profile_id="preview",
            width=540,
            height=960,
            fps=30,
            audio_codec="aac",
            properties=profile_properties,
        ),
        total_duration_seconds=2.0,
        scenes=(scene,),
        audio_tracks=(music, original),
        assets=(
            PlannedAsset(
                asset_id=image_id,
                sha256="a" * 64,
                media_type=MediaType.IMAGE,
                mime_type="image/png",
                width=640,
                height=640,
                has_audio=False,
            ),
            PlannedAsset(
                asset_id=music_id,
                sha256="b" * 64,
                media_type=MediaType.AUDIO,
                mime_type="audio/wav",
                duration_seconds=10.0,
                has_audio=True,
            ),
            PlannedAsset(
                asset_id=original_id,
                sha256="c" * 64,
                media_type=MediaType.AUDIO,
                mime_type="audio/wav",
                duration_seconds=2.0,
                has_audio=True,
            ),
        ),
    )


def _paths(tmp_path: Path) -> tuple[RenderPlan, dict[str, Path]]:
    image_id = new_entity_id(EntityKind.ASSET)
    music_id = new_entity_id(EntityKind.ASSET)
    original_id = new_entity_id(EntityKind.ASSET)
    paths = {
        image_id: tmp_path / "image.png",
        music_id: tmp_path / "music.wav",
        original_id: tmp_path / "original.wav",
    }
    for path in paths.values():
        path.write_bytes(b"synthetic-placeholder")
    return _plan(image_id, music_id, original_id), paths


def _profile_properties(plan: RenderPlan) -> dict[str, object]:
    return plan.output_profile.model_dump(mode="json")["properties"]


def test_audio_wrapper_adds_fades_ducking_limiter_and_evidence(tmp_path: Path) -> None:
    plan, paths = _paths(tmp_path)
    manifest = compile_ffmpeg_command(
        plan, paths, _capabilities(), tmp_path / "out.mp4", prefer_nvenc=False
    )

    assert "afade=t=in:st=0:d=0.2" in manifest.filtergraph
    assert "afade=t=out:st=1.75:d=0.25" in manifest.filtergraph
    assert "volume=-8dB:enable='between(t,0.5,1.25)'" in manifest.filtergraph
    assert "alimiter=limit=" in manifest.filtergraph
    assert ":level=false:latency=true" in manifest.filtergraph
    assert manifest.metadata["audio_policy_backend"] == "pr14_audio_policy_v1"
    assert manifest.metadata["audio_cache_key"] == audio_intermediate_cache_key(plan)
    assert manifest.metadata["audio_normalized"] is False
    index = manifest.arguments.index("-filter_complex")
    assert manifest.arguments[index + 1] == manifest.filtergraph


def test_normalization_fails_closed_without_frozen_measurement(tmp_path: Path) -> None:
    plan, paths = _paths(tmp_path)
    properties = _profile_properties(plan)
    mastering = dict(properties["audio_mastering"])
    mastering["normalize"] = True
    properties["audio_mastering"] = mastering
    profile = plan.output_profile.validated_copy(update={"properties": properties})
    plan = plan.validated_copy(update={"output_profile": profile})
    with pytest.raises(
        UnsupportedRenderFeatureError,
        match="requires frozen first-pass measurement",
    ):
        compile_ffmpeg_command(
            plan, paths, _capabilities(), tmp_path / "out.mp4", prefer_nvenc=False
        )


def test_normalization_uses_frozen_first_pass_measurement(tmp_path: Path) -> None:
    plan, paths = _paths(tmp_path)
    measurement = LoudnessMeasurement(
        input_i=-20.0,
        input_tp=-4.0,
        input_lra=2.5,
        input_thresh=-30.0,
        target_offset=0.1,
    )
    properties = _profile_properties(plan)
    mastering = dict(properties["audio_mastering"])
    mastering.update(
        {"normalize": True, "measurement": measurement.model_dump(mode="json")}
    )
    properties["audio_mastering"] = mastering
    profile = plan.output_profile.validated_copy(update={"properties": properties})
    normalized = plan.validated_copy(update={"output_profile": profile})

    manifest = compile_ffmpeg_command(
        normalized,
        paths,
        _capabilities(),
        tmp_path / "normalized.mp4",
        prefer_nvenc=False,
    )

    assert "loudnorm=I=-14:TP=-1:LRA=11:" in manifest.filtergraph
    assert "measured_I=-20" in manifest.filtergraph
    assert "linear=true" in manifest.filtergraph
    assert manifest.metadata["audio_normalized"] is True


def test_fades_cannot_exceed_track_duration(tmp_path: Path) -> None:
    plan, paths = _paths(tmp_path)
    music = plan.audio_tracks[0].validated_copy(
        update={
            "properties": {
                "fade_in_seconds": 1.5,
                "fade_out_seconds": 1.0,
                "duck_db": -8.0,
            }
        }
    )
    invalid = plan.validated_copy(update={"audio_tracks": (music, plan.audio_tracks[1])})
    with pytest.raises(UnsupportedRenderFeatureError, match="fades exceed"):
        compile_ffmpeg_command(
            invalid,
            paths,
            _capabilities(),
            tmp_path / "invalid.mp4",
            prefer_nvenc=False,
        )


def test_audio_cache_key_ignores_visual_placement_changes(tmp_path: Path) -> None:
    plan, _ = _paths(tmp_path)
    moved_scene = plan.scenes[0].validated_copy(
        update={"placement": NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.8)}
    )
    visually_changed = plan.validated_copy(update={"scenes": (moved_scene,)})
    assert audio_intermediate_cache_key(plan) == audio_intermediate_cache_key(
        visually_changed
    )


def test_no_pr14_properties_delegate_to_pr13_manifest_unchanged(tmp_path: Path) -> None:
    image_id = new_entity_id(EntityKind.ASSET)
    music_id = new_entity_id(EntityKind.ASSET)
    original_id = new_entity_id(EntityKind.ASSET)
    paths = {
        image_id: tmp_path / "image.png",
        music_id: tmp_path / "music.wav",
        original_id: tmp_path / "original.wav",
    }
    for path in paths.values():
        path.write_bytes(b"synthetic-placeholder")
    plan = _plan(
        image_id,
        music_id,
        original_id,
        audio_features=False,
    )
    public = compile_ffmpeg_command(
        plan, paths, _capabilities(), tmp_path / "same.mp4", prefer_nvenc=False
    )
    previous = compile_pre_pr14_command(
        plan, paths, _capabilities(), tmp_path / "same.mp4", prefer_nvenc=False
    )
    assert public == previous
