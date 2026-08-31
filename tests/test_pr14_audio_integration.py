from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from content_forge.audio import (
    AudioMixPolicy,
    compile_loudness_analysis_command,
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
from content_forge.render.ffmpeg import FFmpegBackend, probe_ffmpeg_runtime, probe_media
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
    properties: dict[str, object] = {}
    if mastering is not None:
        properties["audio_mastering"] = mastering
        properties["audio_policy"] = {"policy_id": "integration", "version": "1.0"}
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
    backend = FFmpegBackend(capabilities, paths, prefer_nvenc=False)
    premaster_output = tmp_path / "premaster.mp4"
    premaster_manifest = backend.compile(premaster_plan, premaster_output)
    assert "afade=t=in:" in premaster_manifest.filtergraph
    assert "volume=-7dB:enable='between(t,0.5,1.3)'" in premaster_manifest.filtergraph
    backend.render(premaster_plan, premaster_output, timeout=30)

    analysis = subprocess.run(
        compile_loudness_analysis_command(
            premaster_output,
            ffmpeg_path=capabilities.ffmpeg_path,
            policy=AudioMixPolicy(normalize=True),
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    measurement = parse_loudnorm_measurement(analysis.stderr)

    master = {
        "normalize": True,
        "target_integrated_lufs": -14.0,
        "target_true_peak_dbfs": -1.0,
        "target_lra": 11.0,
        "limiter_dbfs": -1.0,
        "measurement": measurement.model_dump(mode="json"),
    }
    final_plan = _plan(image_id, music_id, original_id, mastering=master)
    final_output = tmp_path / "mastered.mp4"
    final_manifest = backend.compile(final_plan, final_output)
    assert "loudnorm=I=-14:TP=-1:LRA=11:" in final_manifest.filtergraph
    assert "alimiter=limit=" in final_manifest.filtergraph
    assert final_manifest.metadata["audio_normalized"] is True

    result = backend.render(final_plan, final_output, timeout=30)
    assert result.bytes_written > 0
    probe = probe_media(final_output, ffprobe_path=capabilities.ffprobe_path)
    assert probe.has_video is True
    assert probe.has_audio is True
    assert probe.width == 320
    assert probe.height == 568
