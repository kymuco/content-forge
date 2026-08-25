from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import content_forge.render.ffmpeg.probe as probe_module
from content_forge.core import Asset, MediaType, NormalizedRect, OutputProfile
from content_forge.render.ffmpeg import (
    FFmpegCapabilities,
    MediaProbeError,
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


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_ffprobe_capture_rejects_output_beyond_hard_byte_budget(stream_name: str) -> None:
    limit = 8 * 1024
    script = (
        "import sys; "
        f"stream = sys.{stream_name}.buffer; "
        f"stream.write(b'x' * {limit + 1}); "
        "stream.flush()"
    )

    with pytest.raises(MediaProbeError, match="output exceeded safe limit"):
        probe_module._run_ffprobe_bounded(
            (sys.executable, "-c", script),
            timeout=5.0,
            stdout_limit=limit,
            stderr_limit=limit,
        )


def test_ffprobe_interrupt_terminates_child_before_reader_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = threading.Event()

    class BlockingPipe:
        def read(self, size: int) -> bytes:
            assert size > 0
            assert released.wait(timeout=2.0), "reader was joined before child termination"
            return b""

        def close(self) -> None:
            pass

    class InterruptingProcess:
        def __init__(self) -> None:
            self.stdout = BlockingPipe()
            self.stderr = BlockingPipe()
            self.returncode: int | None = None
            self.killed = False
            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            assert self.killed
            self.returncode = -9
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            released.set()

    process = InterruptingProcess()
    monkeypatch.setattr(probe_module.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        probe_module._run_ffprobe_bounded(("synthetic-ffprobe",), timeout=60.0)

    assert process.killed is True
    assert process.wait_calls == 2
    assert released.is_set()


def test_probe_media_requests_only_safe_consumed_ffprobe_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fixture.bin"
    source.write_bytes(b"probe fixture")
    captured: dict[str, object] = {}

    def fake_run(
        arguments: tuple[str, ...],
        *,
        timeout: float,
        stdout_limit: int = probe_module.FFPROBE_STDOUT_LIMIT_BYTES,
        stderr_limit: int = probe_module.FFPROBE_STDERR_LIMIT_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        captured["arguments"] = arguments
        captured["timeout"] = timeout
        captured["stdout_limit"] = stdout_limit
        captured["stderr_limit"] = stderr_limit
        payload = {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "duration": "1.25",
                }
            ],
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.25"},
        }
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(probe_module, "_run_ffprobe_bounded", fake_run)

    probe = probe_media(source, ffprobe_path="synthetic-ffprobe", timeout=3.0)

    arguments = captured["arguments"]
    assert isinstance(arguments, tuple)
    assert "-protocol_whitelist" in arguments
    protocol_index = arguments.index("-protocol_whitelist")
    assert arguments[protocol_index + 1] == "file"
    assert "-format_whitelist" in arguments
    format_index = arguments.index("-format_whitelist")
    assert arguments[format_index + 1] == probe_module._LOCAL_MEDIA_FORMAT_WHITELIST
    allowed_formats = set(probe_module._LOCAL_MEDIA_FORMAT_WHITELIST.split(","))
    assert {"hls", "dash", "concat", "sdp", "m3u", "pls"}.isdisjoint(allowed_formats)
    assert protocol_index < format_index < len(arguments) - 1
    assert "-show_entries" in arguments
    entries = arguments[arguments.index("-show_entries") + 1]
    assert entries == probe_module._FFPROBE_SHOW_ENTRIES
    assert "tag" not in entries.lower()
    assert "-show_streams" not in arguments
    assert "-show_format" not in arguments
    assert captured["timeout"] == 3.0
    assert probe.has_audio is True
    assert probe.has_video is False
    assert probe.audio_codec == "aac"
    assert probe.duration_seconds == 1.25


def test_ffprobe_rejects_hls_nested_local_file_reference(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg runtime is not installed")

    segment = tmp_path / "outside-upload.ts"
    generated = subprocess.run(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x32:r=1:d=1",
            "-c:v",
            "mpeg2video",
            "-f",
            "mpegts",
            str(segment),
        ),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stderr

    # The external target itself is an allowed self-contained media object.
    direct = probe_media(segment, ffprobe_path=ffprobe)
    assert direct.has_video is True

    playlist = tmp_path / "uploaded-reference.m3u8"
    playlist.write_text(
        "\n".join(
            (
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:1",
                "#EXT-X-MEDIA-SEQUENCE:0",
                "#EXTINF:1.0,",
                segment.resolve().as_uri(),
                "#EXT-X-ENDLIST",
                "",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(MediaProbeError, match="ffprobe failed"):
        probe_media(playlist, ffprobe_path=ffprobe)


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
