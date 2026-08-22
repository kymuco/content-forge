from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from content_forge.core import Asset, MediaType, OutputProfile, NormalizedRect
from content_forge.render.ffmpeg import (
    FFmpegCapabilities,
    apply_probe_to_asset,
    parse_encoders,
    parse_filters,
    probe_ffmpeg_runtime,
    probe_media,
    resolve_pixel_rect,
    select_h264_encoder,
)


def test_encoder_and_filter_listing_parsers_are_deterministic() -> None:
    encoders = parse_encoders(
        """
 Encoders:
 V....D libx264              H.264 / AVC
 V....D h264_nvenc           NVIDIA NVENC H.264
 A....D aac                  AAC
"""
    )
    filters = parse_filters(
        """
 Filters:
 ... scale             V->V
 T.. drawtext          V->V
 .SC overlay           VV->V
"""
    )

    assert encoders == ("aac", "h264_nvenc", "libx264")
    assert filters == ("drawtext", "overlay", "scale")


def test_encoder_selection_prefers_usable_nvenc_and_falls_back_to_cpu() -> None:
    base = dict(
        ffmpeg_path="/synthetic/ffmpeg",
        ffprobe_path="/synthetic/ffprobe",
        ffmpeg_version="ffmpeg synthetic",
        ffprobe_version="ffprobe synthetic",
        encoders=("h264_nvenc", "libx264"),
        filters=(),
    )
    hardware = FFmpegCapabilities(**base, h264_nvenc_usable=True)
    software = FFmpegCapabilities(**base, h264_nvenc_usable=False)

    assert select_h264_encoder(hardware) == "h264_nvenc"
    assert select_h264_encoder(software) == "libx264"
    assert select_h264_encoder(hardware, prefer_nvenc=False) == "libx264"


def test_normalized_rect_resolves_from_edges_without_preview_drift() -> None:
    profile = OutputProfile(
        profile_id="preview_vertical",
        width=540,
        height=960,
        fps=30,
        audio_codec=None,
    )
    rect = NormalizedRect(x=1 / 3, y=0.1, width=1 / 3, height=0.5)

    pixels = resolve_pixel_rect(rect, profile)

    assert pixels.x == 180
    assert pixels.y == 96
    assert pixels.width == 180
    assert pixels.height == 480


def test_ffprobe_can_read_synthetic_ppm_and_enrich_asset(tmp_path: Path) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe is not installed")

    source = tmp_path / "fixture.ppm"
    source.write_text(
        "P3\n4 2\n255\n" + "255 0 0\n" * 8,
        encoding="ascii",
    )
    probe = probe_media(source, ffprobe_path=ffprobe)
    assert probe.has_video is True
    assert probe.has_audio is False
    assert probe.width == 4
    assert probe.height == 2

    asset = Asset(
        sha256="a" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
        size_bytes=source.stat().st_size,
    )
    enriched = apply_probe_to_asset(asset, probe)
    assert enriched.width == 4
    assert enriched.height == 2
    assert enriched.duration_seconds is None
    assert enriched.has_audio is False


def test_runtime_probe_reports_cpu_encoder_when_available() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg runtime is not installed")

    capabilities = probe_ffmpeg_runtime(test_nvenc=False)

    assert Path(capabilities.ffmpeg_path).is_file()
    assert Path(capabilities.ffprobe_path).is_file()
    assert capabilities.ffmpeg_version.startswith("ffmpeg version")
    assert "scale" in capabilities.filters
