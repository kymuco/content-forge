from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from content_forge.audio import (
    AudioIntermediateCache,
    AudioMixPolicy,
    audio_intermediate_cache_key,
    compile_loudness_analysis_command,
    evaluate_audio_qc,
    parse_loudnorm_measurement,
)
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
    compile_audio_intermediate_command,
    probe_ffmpeg_runtime,
    probe_media,
)
from content_forge.timeline import PlannedAsset, PlannedAudioTrack, PlannedScene, RenderPlan


def _ppm(path: Path) -> None:
    path.write_text(
        "P3\n64 48\n255\n" + "\n".join(["40 80 160"] * (64 * 48)) + "\n",
        encoding="ascii",
    )


def _tone(path: Path, *, frequency: float, seconds: float, amplitude: float) -> None:
    sample_rate = 48000
    frame_count = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            value = int(
                max(
                    -1.0,
                    min(
                        1.0,
                        amplitude
                        * math.sin(2.0 * math.pi * frequency * index / sample_rate),
                    ),
                )
                * 32767
            )
            frames.extend(struct.pack("<hh", value, value))
        handle.writeframes(bytes(frames))


def _capabilities():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg runtime is not installed")
    capabilities = probe_ffmpeg_runtime(test_nvenc=False)
    if not capabilities.has_libx264:
        pytest.skip("integration fixture requires libx264 CPU fallback")
    required = {"afade", "alimiter", "loudnorm", "volume"}
    if not required.issubset(set(capabilities.filters)):
        pytest.skip("FFmpeg runtime lacks PR14 audio filters")
    return capabilities


def _plan(
    image_id: str,
    music_id: str,
    original_id: str,
    *,
    mastering: dict[str, object] | None = None,
) -> RenderPlan:
    properties: dict[str, object] = {
        "audio_policy": {"policy_id": "integration", "version": "1.0"}
    }
    if mastering is not None:
        properties["audio_mastering"] = mastering
    return RenderPlan(
        project_id=new_entity_id(EntityKind.PROJECT),
        output_profile=OutputProfile(
            profile_id="audio_preview",
            width=320,
            height=568,
            fps=24,
            audio_codec="aac",
            properties=properties,
        ),
        total_duration_seconds=2.0,
        scenes=(
            PlannedScene(
                scene_id=new_entity_id(EntityKind.SCENE),
                order=0,
                start_seconds=0.0,
                duration_seconds=2.0,
                end_seconds=2.0,
                media_asset_id=image_id,
                placement=NormalizedRect(x=0, y=0, width=1, height=1),
                fit_mode=FitMode.COVER,
            ),
        ),
        audio_tracks=(
            PlannedAudioTrack(
                audio_track_id=new_entity_id(EntityKind.AUDIO),
                track_type="music",
                start_seconds=0.0,
                duration_seconds=2.0,
                end_seconds=2.0,
                asset_id=music_id,
                gain_db=-8.0,
                loop=True,
                properties={
                    "fade_in_seconds": 0.15,
                    "fade_out_seconds": 0.2,
                    "duck_db": -7.0,
                },
            ),
            PlannedAudioTrack(
                audio_track_id=new_entity_id(EntityKind.AUDIO),
                track_type="original",
                start_seconds=0.5,
                duration_seconds=0.8,
                end_seconds=1.3,
                asset_id=original_id,
                gain_db=0.0,
            ),
        ),
        assets=(
            PlannedAsset(
                asset_id=image_id,
                sha256="a" * 64,
                media_type=MediaType.IMAGE,
                mime_type="image/x-portable-pixmap",
                width=64,
                height=48,
                has_audio=False,
            ),
            PlannedAsset(
                asset_id=music_id,
                sha256="b" * 64,
                media_type=MediaType.AUDIO,
                mime_type="audio/wav",
                duration_seconds=4.0,
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


def test_pr14_mix_and_two_pass_mastering_render_through_real_ffmpeg(tmp_path: Path) -> None:
    capabilities = _capabilities()
    image = tmp_path / "image.ppm"
    music = tmp_path / "music.wav"
    original = tmp_path / "original.wav"
    _ppm(image)
    _tone(music, frequency=220.0, seconds=4.0, amplitude=0.18)
    _tone(original, frequency=660.0, seconds=2.0, amplitude=0.35)

    image_id = new_entity_id(EntityKind.ASSET)
    music_id = new_entity_id(EntityKind.ASSET)
    original_id = new_entity_id(EntityKind.ASSET)
    paths = {image_id: image, music_id: music, original_id: original}

    premaster_plan = _plan(image_id, music_id, original_id)
    premaster_output = tmp_path / "premaster.wav"
    audio_only_paths = {music_id: music, original_id: original}
    premaster_command = compile_audio_intermediate_command(
        premaster_plan,
        audio_only_paths,
        capabilities,
        premaster_output,
        prefer_nvenc=False,
    )
    filter_index = premaster_command.index("-filter_complex")
    premaster_filtergraph = premaster_command[filter_index + 1]
    assert "afade=t=in:" in premaster_filtergraph
    assert "volume=-7dB:enable='between(t,0.5,1.3)'" in premaster_filtergraph
    assert "[vout]" not in premaster_filtergraph
    assert "pcm_f32le" in premaster_command
    assert str(image.resolve()) not in premaster_command
    subprocess.run(
        premaster_command,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    premaster_probe = probe_media(
        premaster_output, ffprobe_path=capabilities.ffprobe_path
    )
    assert premaster_probe.has_audio is True
    assert premaster_probe.has_video is False

    cache_key = audio_intermediate_cache_key(premaster_plan)
    cache = AudioIntermediateCache(tmp_path / "audio-cache")
    cached_premaster = cache.publish(premaster_output, cache_key)
    assert cached_premaster.read_bytes() == premaster_output.read_bytes()

    analysis = subprocess.run(
        compile_loudness_analysis_command(
            cached_premaster,
            ffmpeg_path=capabilities.ffmpeg_path,
            policy=AudioMixPolicy(normalize=True),
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    measurement = parse_loudnorm_measurement(analysis.stderr)
    assert measurement.normalizable is True

    master = {
        "normalize": True,
        "target_integrated_lufs": -14.0,
        "target_true_peak_dbfs": -1.0,
        "target_lra": 11.0,
        "limiter_dbfs": -1.0,
        "measurement": measurement.model_dump(mode="json"),
    }
    profile_properties = premaster_plan.output_profile.model_dump(mode="json")["properties"]
    profile_properties["audio_mastering"] = master
    mastered_profile = premaster_plan.output_profile.validated_copy(
        update={"properties": profile_properties}
    )
    final_plan = premaster_plan.validated_copy(
        update={"output_profile": mastered_profile}
    )
    assert audio_intermediate_cache_key(final_plan) == cache_key

    backend = FFmpegBackend(capabilities, paths, prefer_nvenc=False)
    final_output = tmp_path / "mastered.mp4"
    final_manifest = backend.compile(final_plan, final_output)
    assert "loudnorm=I=-14:TP=-1:LRA=11:" in final_manifest.filtergraph
    assert "alimiter=limit=" in final_manifest.filtergraph
    assert final_manifest.metadata["audio_normalized"] is True
    assert final_manifest.metadata["audio_cache_key"] == cache_key

    result = backend.render(final_plan, final_output, timeout=30)
    assert result.bytes_written > 0
    probe = probe_media(final_output, ffprobe_path=capabilities.ffprobe_path)
    assert probe.has_video is True
    assert probe.has_audio is True
    assert probe.width == 320
    assert probe.height == 568


def test_real_loudnorm_silence_is_qc_evidence_not_normalization_input(tmp_path: Path) -> None:
    capabilities = _capabilities()
    silence = tmp_path / "silence.wav"
    _tone(silence, frequency=440.0, seconds=1.0, amplitude=0.0)

    analysis = subprocess.run(
        compile_loudness_analysis_command(
            silence,
            ffmpeg_path=capabilities.ffmpeg_path,
            policy=AudioMixPolicy(normalize=True),
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    measurement = parse_loudnorm_measurement(analysis.stderr)
    qc = evaluate_audio_qc(measurement, AudioMixPolicy())

    assert measurement.silent_sentinel is True
    assert measurement.normalizable is False
    assert qc.silent is True
    assert qc.passed is False
