from __future__ import annotations

import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    AudioTrack,
    EntityKind,
    FitMode,
    MediaType,
    MotionSpec,
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
                amplitude
                * math.sin(2.0 * math.pi * frequency * index / sample_rate)
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
    return capabilities


def test_pr23_focus_zoom_renders_through_public_ffmpeg_backend(tmp_path: Path) -> None:
    source = tmp_path / "panel.ppm"
    _ppm(source)
    asset = Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="d" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
        size_bytes=source.stat().st_size,
        width=64,
        height=48,
        has_audio=False,
    )
    profile = shorts_preview_profile()
    project = Project(
        content_kind="pr23_focus_zoom_fixture",
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.45,
                media=AssetRef(asset_id=asset.asset_id),
                placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
                fit_mode=FitMode.COVER,
                motion=MotionSpec(
                    motion_type="focus_zoom",
                    focus=NormalizedPoint(x=0.62, y=0.42),
                    properties={
                        "pr23_owner": "pr23_camera_v1",
                        "focus_source": "face_hint",
                        "start_scale": 0.86,
                        "end_scale": 0.74,
                    },
                ),
            ),
        ),
        output_profiles=(profile,),
    )
    plan = compile_timeline(
        project,
        {asset.asset_id: asset},
        profile_id=profile.profile_id,
    )
    output = tmp_path / "focus-zoom.mp4"
    backend = FFmpegBackend(
        _capabilities(),
        {asset.asset_id: source},
        prefer_nvenc=False,
    )

    manifest = backend.compile(plan, output)
    assert manifest.metadata["presentation_backend"] == "pr23_v1"
    assert manifest.metadata["focus_zoom_scene_count"] == 1
    assert "crop=540:960:" in manifest.filtergraph
    assert "min(max(0.62-" in manifest.filtergraph

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


def test_pr23_ambience_duck_renders_through_public_ffmpeg_backend(tmp_path: Path) -> None:
    image_path = tmp_path / "panel.ppm"
    voice_path = tmp_path / "voice.wav"
    ambience_path = tmp_path / "ambience.wav"
    _ppm(image_path)
    _tone(voice_path, frequency=660.0, seconds=0.5, amplitude=0.25)
    _tone(ambience_path, frequency=220.0, seconds=1.2, amplitude=0.12)

    image = Asset(
        sha256="a" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
        size_bytes=image_path.stat().st_size,
        width=64,
        height=48,
        has_audio=False,
    )
    voice = Asset(
        sha256="b" * 64,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
        size_bytes=voice_path.stat().st_size,
        duration_seconds=0.5,
        has_audio=True,
    )
    ambience = Asset(
        sha256="c" * 64,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
        size_bytes=ambience_path.stat().st_size,
        duration_seconds=1.2,
        has_audio=True,
    )
    profile = shorts_preview_profile()
    project = Project(
        content_kind="pr23_ambience_duck_fixture",
        scenes=(
            Scene(
                order=0,
                duration_seconds=1.2,
                media=AssetRef(asset_id=image.asset_id),
                audio_tracks=(
                    AudioTrack(
                        track_type="voice",
                        asset_ref=AssetRef(asset_id=voice.asset_id, role="voice"),
                        start_seconds=0.25,
                        duration_seconds=0.5,
                    ),
                    AudioTrack(
                        track_type="ambience",
                        asset_ref=AssetRef(asset_id=ambience.asset_id, role="ambience"),
                        start_seconds=0.0,
                        duration_seconds=1.2,
                        gain_db=-3.0,
                        properties={
                            "pr23_owner": "pr23_voiced_mix_v1",
                            "pr23_preset_id": "natural_dialogue",
                            "pr23_preset_version": "1",
                            "duck_db": -6.0,
                        },
                    ),
                ),
            ),
        ),
        output_profiles=(profile,),
    )
    assets = {
        image.asset_id: image,
        voice.asset_id: voice,
        ambience.asset_id: ambience,
    }
    plan = compile_timeline(project, assets, profile_id=profile.profile_id)
    output = tmp_path / "ambience-duck.mp4"
    backend = FFmpegBackend(
        _capabilities(),
        {
            image.asset_id: image_path,
            voice.asset_id: voice_path,
            ambience.asset_id: ambience_path,
        },
        prefer_nvenc=False,
    )

    manifest = backend.compile(plan, output)
    assert manifest.metadata["presentation_backend"] == "pr23_v1"
    assert manifest.metadata["ambience_duck_track_count"] == 1
    assert "volume=-6dB:enable='between(t,0.25,0.75)'" in manifest.filtergraph

    result = backend.render(plan, output, timeout=20)
    assert result.bytes_written > 0
    probe = probe_media(output, ffprobe_path=backend.capabilities.ffprobe_path)
    assert probe.has_video is True
    assert probe.has_audio is True
    assert probe.width == 540
    assert probe.height == 960
    assert probe.duration_seconds is not None
    assert 1.0 <= probe.duration_seconds <= 1.5
