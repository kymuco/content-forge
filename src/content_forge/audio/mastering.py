"""Deterministic PR14 loudness analysis, mastering, QC, and audio cache identity."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from content_forge.timeline import RenderPlan

from .models import AudioMixPolicy, AudioQCResult, LoudnessMeasurement

_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def _number(value: float) -> str:
    text = f"{float(value):.9f}".rstrip("0").rstrip(".")
    return text or "0"


def loudness_analysis_filter(policy: AudioMixPolicy) -> str:
    return (
        "loudnorm="
        f"I={_number(policy.target_integrated_lufs)}:"
        f"TP={_number(policy.target_true_peak_dbfs)}:"
        f"LRA={_number(policy.target_lra)}:"
        "print_format=json"
    )


def loudness_apply_filter(
    policy: AudioMixPolicy,
    measurement: LoudnessMeasurement,
) -> str:
    """Build the second loudnorm pass from frozen first-pass evidence."""

    return (
        "loudnorm="
        f"I={_number(policy.target_integrated_lufs)}:"
        f"TP={_number(policy.target_true_peak_dbfs)}:"
        f"LRA={_number(policy.target_lra)}:"
        f"measured_I={_number(measurement.input_i)}:"
        f"measured_TP={_number(measurement.input_tp)}:"
        f"measured_LRA={_number(measurement.input_lra)}:"
        f"measured_thresh={_number(measurement.input_thresh)}:"
        f"offset={_number(measurement.target_offset)}:"
        "linear=true:print_format=summary"
    )


def compile_loudness_analysis_command(
    input_path: str | Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    policy: AudioMixPolicy | None = None,
) -> tuple[str, ...]:
    selected = policy or AudioMixPolicy(normalize=True)
    return (
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(Path(input_path)),
        "-vn",
        "-af",
        loudness_analysis_filter(selected),
        "-f",
        "null",
        "-",
    )


def parse_loudnorm_measurement(stderr: str) -> LoudnessMeasurement:
    matches = list(_LOUDNORM_JSON.finditer(stderr))
    if not matches:
        raise ValueError("FFmpeg loudnorm JSON measurement was not found")
    payload = json.loads(matches[-1].group(0))
    try:
        return LoudnessMeasurement(
            input_i=float(payload["input_i"]),
            input_tp=float(payload["input_tp"]),
            input_lra=float(payload["input_lra"]),
            input_thresh=float(payload["input_thresh"]),
            target_offset=float(payload["target_offset"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid FFmpeg loudnorm measurement payload") from exc


def evaluate_audio_qc(
    measurement: LoudnessMeasurement,
    policy: AudioMixPolicy,
    *,
    loudness_tolerance_lu: float = 1.0,
    peak_tolerance_db: float = 0.1,
    silence_floor_lufs: float = -70.0,
) -> AudioQCResult:
    silent = measurement.input_i <= silence_floor_lufs
    return AudioQCResult(
        integrated_lufs=measurement.input_i,
        true_peak_dbfs=measurement.input_tp,
        loudness_range_lu=measurement.input_lra,
        silent=silent,
        loudness_ok=(
            abs(measurement.input_i - policy.target_integrated_lufs)
            <= loudness_tolerance_lu
        ),
        true_peak_ok=(
            measurement.input_tp
            <= policy.target_true_peak_dbfs + peak_tolerance_db
        ),
    )


def _audio_track_payload(plan: RenderPlan) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for track in sorted(plan.audio_tracks, key=lambda item: item.audio_track_id):
        data = track.model_dump(mode="json")
        payloads.append(
            {
                "audio_track_id": data["audio_track_id"],
                "track_type": data["track_type"],
                "scope_scene_id": data["scope_scene_id"],
                "start_seconds": data["start_seconds"],
                "duration_seconds": data["duration_seconds"],
                "end_seconds": data["end_seconds"],
                "asset_id": data["asset_id"],
                "source_id": data["source_id"],
                "source_start_seconds": data["source_start_seconds"],
                "gain_db": data["gain_db"],
                "loop": data["loop"],
                "properties": data["properties"],
            }
        )
    return payloads


def audio_intermediate_cache_key(plan: RenderPlan) -> str:
    """Hash only audio-affecting evidence so visual edits do not invalidate audio."""

    asset_by_id = {asset.asset_id: asset for asset in plan.assets}
    audio_asset_ids = {
        track.asset_id for track in plan.audio_tracks if track.asset_id is not None
    }
    profile = plan.output_profile.model_dump(mode="json")
    profile_properties = profile["properties"]
    payload = {
        "version": "pr14_audio_cache_v1",
        "duration_seconds": plan.total_duration_seconds,
        "tracks": _audio_track_payload(plan),
        "assets": [
            {
                "asset_id": asset_id,
                "sha256": asset_by_id[asset_id].sha256,
                "duration_seconds": asset_by_id[asset_id].duration_seconds,
            }
            for asset_id in sorted(audio_asset_ids)
        ],
        "audio_codec": profile["audio_codec"],
        "audio_bitrate_kbps": profile["audio_bitrate_kbps"],
        "mastering": profile_properties.get("audio_mastering"),
        "policy": profile_properties.get("audio_policy"),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "audio_intermediate_cache_key",
    "compile_loudness_analysis_command",
    "evaluate_audio_qc",
    "loudness_analysis_filter",
    "loudness_apply_filter",
    "parse_loudnorm_measurement",
]
