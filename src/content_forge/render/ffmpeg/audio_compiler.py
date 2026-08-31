"""PR14 audio-aware wrapper over the PR13 motion-aware FFmpeg compiler."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

from content_forge.audio import AudioMixPolicy, LoudnessMeasurement
from content_forge.audio.mastering import (
    audio_intermediate_cache_key,
    loudness_apply_filter,
)
from content_forge.timeline import RenderPlan

from .capabilities import require_filters
from .compiler import (
    AssetPathSource,
    FFmpegCompileError,
    MissingRenderAssetError,
    RuntimeStorageResolver,
    UnsupportedRenderFeatureError,
)
from .models import FFmpegCapabilities, RenderCommandManifest
from .motion_compiler import compile_ffmpeg_command as _compile_motion_ffmpeg_command

_AUDIO_POLICY_VERSION = "pr14_audio_policy_v1"
_DUCK_TRIGGER_TYPES = frozenset({"original", "dialogue", "narration", "voice"})


def _number(value: float) -> str:
    text = f"{float(value):.9f}".rstrip("0").rstrip(".")
    return text or "0"


def _numeric_property(
    value: object,
    *,
    name: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if value is None:
        result = default
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FFmpegCompileError(f"invalid audio property {name}")
    else:
        result = float(value)
    if not math.isfinite(result):
        raise FFmpegCompileError(f"invalid audio property {name}")
    if minimum is not None and result < minimum:
        raise FFmpegCompileError(f"audio property {name} is below minimum")
    if maximum is not None and result > maximum:
        raise FFmpegCompileError(f"audio property {name} exceeds maximum")
    return result


def _audio_part_index(parts: list[str], audio_index: int) -> int:
    suffix = f"[audio_{audio_index}]"
    matches = [index for index, part in enumerate(parts) if part.endswith(suffix)]
    if len(matches) != 1:
        raise FFmpegCompileError(
            f"cannot identify unique fitted audio stream {audio_index}"
        )
    return matches[0]


def _append_before_label(part: str, label: str, filters: list[str]) -> str:
    if not filters:
        return part
    if not part.endswith(label):
        raise FFmpegCompileError(f"unexpected audio filtergraph fragment for {label}")
    return part[: -len(label)] + "," + ",".join(filters) + label


def _duck_expression(plan: RenderPlan, music_index: int) -> str | None:
    music = plan.audio_tracks[music_index]
    intervals: list[tuple[float, float]] = []
    for track in plan.audio_tracks:
        if track.track_type not in _DUCK_TRIGGER_TYPES:
            continue
        start = max(music.start_seconds, track.start_seconds)
        end = min(music.end_seconds, track.end_seconds)
        if end - start > 1e-9:
            intervals.append((start, end))
    if not intervals:
        return None
    return "+".join(
        f"between(t,{_number(start)},{_number(end)})" for start, end in intervals
    )


def _rewrite_tracks(
    plan: RenderPlan,
    parts: list[str],
) -> tuple[set[str], bool]:
    required: set[str] = set()
    changed = False
    for index, track in enumerate(plan.audio_tracks):
        filters: list[str] = []
        fade_in = _numeric_property(
            track.properties.get("fade_in_seconds"),
            name="fade_in_seconds",
            default=0.0,
            minimum=0.0,
        )
        fade_out = _numeric_property(
            track.properties.get("fade_out_seconds"),
            name="fade_out_seconds",
            default=0.0,
            minimum=0.0,
        )
        if fade_in + fade_out - track.duration_seconds > 1e-9:
            raise UnsupportedRenderFeatureError(
                f"audio fades exceed track duration: {track.audio_track_id}"
            )
        if fade_in > 0.0:
            required.add("afade")
            filters.append(
                f"afade=t=in:st={_number(track.start_seconds)}:d={_number(fade_in)}"
            )
        if fade_out > 0.0:
            required.add("afade")
            filters.append(
                f"afade=t=out:st={_number(track.end_seconds - fade_out)}:"
                f"d={_number(fade_out)}"
            )

        if track.track_type == "music":
            duck_db = _numeric_property(
                track.properties.get("duck_db"),
                name="duck_db",
                default=0.0,
                minimum=-60.0,
                maximum=0.0,
            )
            expression = _duck_expression(plan, index)
            if duck_db < 0.0 and expression is not None:
                required.add("volume")
                filters.append(
                    f"volume={_number(duck_db)}dB:enable='{expression}'"
                )

        if filters:
            part_index = _audio_part_index(parts, index)
            label = f"[audio_{index}]"
            parts[part_index] = _append_before_label(
                parts[part_index],
                label,
                filters,
            )
            changed = True
    return required, changed


def _mastering_settings(
    plan: RenderPlan,
) -> tuple[AudioMixPolicy | None, LoudnessMeasurement | None, float | None]:
    raw = plan.output_profile.properties.get("audio_mastering")
    if raw is None:
        return None, None, None
    if not isinstance(raw, Mapping):
        raise FFmpegCompileError("audio_mastering profile property must be an object")

    normalize = raw.get("normalize", False)
    if not isinstance(normalize, bool):
        raise FFmpegCompileError("audio_mastering.normalize must be boolean")

    policy = AudioMixPolicy(
        normalize=normalize,
        target_integrated_lufs=_numeric_property(
            raw.get("target_integrated_lufs"),
            name="target_integrated_lufs",
            default=-14.0,
            minimum=-36.0,
            maximum=-5.0,
        ),
        target_true_peak_dbfs=_numeric_property(
            raw.get("target_true_peak_dbfs"),
            name="target_true_peak_dbfs",
            default=-1.0,
            minimum=-9.0,
            maximum=0.0,
        ),
        target_lra=_numeric_property(
            raw.get("target_lra"),
            name="target_lra",
            default=11.0,
            minimum=1.0,
            maximum=20.0,
        ),
        limiter_dbfs=_numeric_property(
            raw.get("limiter_dbfs"),
            name="limiter_dbfs",
            default=-1.0,
            minimum=-12.0,
            maximum=0.0,
        ),
    )

    measurement_raw = raw.get("measurement")
    measurement = None
    if measurement_raw is not None:
        if not isinstance(measurement_raw, Mapping):
            raise FFmpegCompileError(
                "audio_mastering.measurement must be an object"
            )
        measurement = LoudnessMeasurement.model_validate(dict(measurement_raw))
    if normalize and measurement is None:
        raise UnsupportedRenderFeatureError(
            "two-pass loudness normalization requires frozen first-pass measurement"
        )
    return policy, measurement, policy.limiter_dbfs


def _audio_output_label(arguments: tuple[str, ...]) -> str | None:
    maps = [
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == "-map"
    ]
    if len(maps) < 2:
        return None
    value = maps[-1]
    if not value.startswith("[") or not value.endswith("]"):
        raise FFmpegCompileError("unexpected audio map label")
    return value


def _rewrite_master(
    plan: RenderPlan,
    parts: list[str],
    manifest: RenderCommandManifest,
) -> tuple[set[str], bool, bool]:
    label = _audio_output_label(manifest.arguments)
    if label is None or not plan.audio_tracks:
        return set(), False, False

    policy, measurement, limiter_dbfs = _mastering_settings(plan)
    if policy is None:
        return set(), False, False

    matches = [index for index, part in enumerate(parts) if part.endswith(label)]
    if len(matches) != 1:
        raise FFmpegCompileError("cannot identify unique final audio mix stream")

    filters: list[str] = []
    required: set[str] = set()
    normalized = False
    if policy.normalize:
        assert measurement is not None
        required.add("loudnorm")
        filters.append(loudness_apply_filter(policy, measurement))
        normalized = True

    if limiter_dbfs is not None:
        required.add("alimiter")
        linear_limit = 10.0 ** (limiter_dbfs / 20.0)
        filters.append(
            f"alimiter=limit={_number(linear_limit)}:level=false:latency=true"
        )

    if filters:
        index = matches[0]
        parts[index] = _append_before_label(parts[index], label, filters)
        return required, True, normalized
    return required, False, normalized


def _replace_filter_complex(
    arguments: tuple[str, ...],
    *,
    old_filtergraph: str,
    new_filtergraph: str,
) -> tuple[str, ...]:
    values = list(arguments)
    try:
        index = values.index("-filter_complex")
    except ValueError as exc:
        raise FFmpegCompileError("manifest has no filter_complex argument") from exc
    if index + 1 >= len(values) or values[index + 1] != old_filtergraph:
        raise FFmpegCompileError(
            "manifest filter_complex argument does not match filtergraph"
        )
    values[index + 1] = new_filtergraph
    return tuple(values)


def compile_ffmpeg_command(
    plan: RenderPlan,
    asset_paths: AssetPathSource,
    capabilities: FFmpegCapabilities,
    output_path: str | Path,
    *,
    prefer_nvenc: bool = True,
) -> RenderCommandManifest:
    """Compile PR14 audio policy after PR13 motion and PR5 base semantics."""

    manifest = _compile_motion_ffmpeg_command(
        plan,
        asset_paths,
        capabilities,
        output_path,
        prefer_nvenc=prefer_nvenc,
    )
    if plan.output_profile.audio_codec is None or not plan.audio_tracks:
        return manifest

    parts = manifest.filtergraph.split(";")
    track_filters, track_changed = _rewrite_tracks(plan, parts)
    master_filters, master_changed, normalized = _rewrite_master(
        plan, parts, manifest
    )
    required = track_filters | master_filters
    if required:
        require_filters(capabilities, required)

    if not track_changed and not master_changed:
        return manifest

    filtergraph = ";".join(parts)
    metadata = dict(manifest.metadata)
    metadata.update(
        {
            "audio_policy_backend": _AUDIO_POLICY_VERSION,
            "audio_cache_key": audio_intermediate_cache_key(plan),
            "audio_track_count": len(plan.audio_tracks),
            "audio_normalized": normalized,
        }
    )
    return manifest.validated_copy(
        update={
            "filtergraph": filtergraph,
            "arguments": _replace_filter_complex(
                manifest.arguments,
                old_filtergraph=manifest.filtergraph,
                new_filtergraph=filtergraph,
            ),
            "metadata": metadata,
        }
    )


__all__ = [
    "AssetPathSource",
    "FFmpegCompileError",
    "MissingRenderAssetError",
    "RuntimeStorageResolver",
    "UnsupportedRenderFeatureError",
    "compile_ffmpeg_command",
]
