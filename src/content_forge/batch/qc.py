"""PR17 post-render QC over persisted PR7 plans and verified artifacts."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field

from content_forge.audio import (
    AudioMixPolicy,
    compile_loudness_analysis_command,
    evaluate_audio_qc,
    parse_loudnorm_measurement,
)
from content_forge.core.models import FrozenModel
from content_forge.orchestration import RenderArtifactManifest
from content_forge.timeline import RenderPlan

from .models import QCCheckResult, RenderQCReport

_BLACK_EVENT = re.compile(
    r"black_start:(?P<start>[-+0-9.eE]+)\s+"
    r"black_end:(?P<end>[-+0-9.eE]+)\s+"
    r"black_duration:(?P<duration>[-+0-9.eE]+)"
)


class BlackFrameAnalysis(FrozenModel):
    black_duration_seconds: float = Field(ge=0.0)
    black_ratio: float = Field(ge=0.0, le=1.0)
    event_count: int = Field(ge=0)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rectangles_overlap(left: object, right: object, *, pad_x: float, pad_y: float) -> bool:
    return (
        left.x - pad_x < right.x + right.width
        and right.x < left.x + left.width + pad_x
        and left.y - pad_y < right.y + right.height
        and right.y < left.y + left.height + pad_y
    )


def analyze_black_frames(
    output_path: str | Path,
    *,
    duration_seconds: float,
    ffmpeg_path: str = "ffmpeg",
    minimum_black_seconds: float = 0.20,
    pixel_threshold: float = 0.10,
    timeout: float = 60.0,
) -> BlackFrameAnalysis:
    """Measure sustained black intervals with FFmpeg's blackdetect filter."""

    if duration_seconds <= 0.0:
        raise ValueError("black-frame QC requires a positive artifact duration")
    command = (
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(Path(output_path)),
        "-an",
        "-vf",
        f"blackdetect=d={minimum_black_seconds}:pix_th={pixel_threshold}",
        "-f",
        "null",
        "-",
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip()[-4096:] or "blackdetect failed"
        raise RuntimeError(message)
    durations = [max(0.0, float(match.group("duration"))) for match in _BLACK_EVENT.finditer(completed.stderr)]
    total = min(duration_seconds, sum(durations))
    return BlackFrameAnalysis(
        black_duration_seconds=total,
        black_ratio=min(1.0, total / duration_seconds),
        event_count=len(durations),
    )


def _dimension_check(plan: RenderPlan, artifact: RenderArtifactManifest) -> QCCheckResult:
    expected = (plan.output_profile.width, plan.output_profile.height)
    actual = (artifact.width, artifact.height)
    return QCCheckResult(
        name="dimensions",
        status="pass" if actual == expected else "fail",
        message=(
            "artifact dimensions match the frozen output profile"
            if actual == expected
            else "artifact dimensions differ from the frozen output profile"
        ),
        details={"expected": list(expected), "actual": list(actual)},
    )


def _duration_check(plan: RenderPlan, artifact: RenderArtifactManifest) -> QCCheckResult:
    fps = artifact.fps or plan.output_profile.fps
    tolerance = max(0.10, 2.0 / fps)
    drift = abs(artifact.duration_seconds - plan.total_duration_seconds)
    return QCCheckResult(
        name="duration",
        status="pass" if drift <= tolerance else "fail",
        message=(
            "artifact duration is within the frozen timeline tolerance"
            if drift <= tolerance
            else "artifact duration differs from the frozen timeline"
        ),
        details={
            "expected_seconds": plan.total_duration_seconds,
            "actual_seconds": artifact.duration_seconds,
            "drift_seconds": drift,
            "tolerance_seconds": tolerance,
        },
    )


def _asset_check(plan: RenderPlan, artifact: RenderArtifactManifest) -> QCCheckResult:
    expected = [(item.asset_id, item.sha256) for item in sorted(plan.assets, key=lambda value: value.asset_id)]
    actual = [(item.asset_id, item.sha256) for item in artifact.source_assets]
    return QCCheckResult(
        name="source_assets",
        status="pass" if actual == expected else "fail",
        message=(
            "artifact source fingerprints match the frozen render plan"
            if actual == expected
            else "artifact source fingerprints differ from the frozen render plan"
        ),
        details={"expected_count": len(expected), "actual_count": len(actual)},
    )


def _audio_presence_check(plan: RenderPlan, artifact: RenderArtifactManifest) -> QCCheckResult:
    expected_audio = bool(plan.audio_tracks)
    ok = artifact.has_audio if expected_audio else True
    return QCCheckResult(
        name="audio_presence",
        status="pass" if ok else "fail",
        message=(
            "artifact audio presence satisfies the frozen timeline"
            if ok
            else "timeline contains audio tracks but artifact has no audio stream"
        ),
        details={"audio_tracks": len(plan.audio_tracks), "artifact_has_audio": artifact.has_audio},
    )


def _overflow_check(plan: RenderPlan) -> QCCheckResult:
    text_overlays = [item for item in plan.overlays if item.text is not None]
    if not text_overlays:
        return QCCheckResult(
            name="text_overflow",
            status="pass",
            message="render plan has no text overlays requiring overflow QC",
            details={"text_overlays": 0},
        )

    evaluated = 0
    failures: list[str] = []
    for overlay in text_overlays:
        properties = overlay.properties
        region_w = _number(properties.get("layout_region_width_pixels"))
        required_w = _number(properties.get("layout_required_width_pixels"))
        region_h = _number(properties.get("layout_region_height_pixels"))
        required_h = _number(properties.get("layout_required_height_pixels"))
        if None not in {region_w, required_w, region_h, required_h}:
            evaluated += 1
            if required_w > region_w or required_h > region_h:  # type: ignore[operator]
                failures.append(overlay.overlay_id)

    if (
        plan.template_id == "hook_overlay"
        and len(text_overlays) == 1
        and evaluated == 0
    ):
        props = plan.template_properties
        region_w = _number(props.get("hook_region_width_pixels"))
        required_w = _number(props.get("hook_required_width_pixels"))
        region_h = _number(props.get("hook_region_height_pixels"))
        required_h = _number(props.get("hook_required_height_pixels"))
        if None not in {region_w, required_w, region_h, required_h}:
            evaluated = 1
            if required_w > region_w or required_h > region_h:  # type: ignore[operator]
                failures.append(text_overlays[0].overlay_id)

    if failures:
        return QCCheckResult(
            name="text_overflow",
            status="fail",
            message="one or more text overlays exceed their frozen layout budget",
            details={"overlay_ids": failures, "evaluated": evaluated, "total": len(text_overlays)},
        )
    if evaluated != len(text_overlays):
        return QCCheckResult(
            name="text_overflow",
            status="not_evaluable",
            blocking=False,
            message="some text overlays do not carry deterministic layout-budget evidence",
            details={"evaluated": evaluated, "total": len(text_overlays)},
        )
    return QCCheckResult(
        name="text_overflow",
        status="pass",
        message="all text overlays fit their frozen deterministic layout budgets",
        details={"evaluated": evaluated, "total": len(text_overlays)},
    )


def _safe_zone_check(plan: RenderPlan) -> QCCheckResult:
    text_overlays = [item for item in plan.overlays if item.text is not None]
    if not text_overlays or not plan.output_profile.safe_zones:
        return QCCheckResult(
            name="safe_zones",
            status="pass",
            message="no text/safe-zone intersections require QC",
            details={
                "text_overlays": len(text_overlays),
                "safe_zones": len(plan.output_profile.safe_zones),
            },
        )
    collisions: list[str] = []
    for overlay in text_overlays:
        border = _number(overlay.properties.get("border_width")) or 0.0
        multiplier = 2.0 if overlay.properties.get("box") is True else 1.0
        pad_x = border * multiplier / plan.output_profile.width
        pad_y = border * multiplier / plan.output_profile.height
        for zone in plan.output_profile.safe_zones:
            if _rectangles_overlap(overlay.placement, zone.rect, pad_x=pad_x, pad_y=pad_y):
                collisions.append(f"{overlay.overlay_id}:{zone.name}")
    return QCCheckResult(
        name="safe_zones",
        status="fail" if collisions else "pass",
        message=(
            "text placement intersects a protected output safe zone"
            if collisions
            else "text placement and border footprint avoid protected safe zones"
        ),
        details={"collisions": collisions},
    )


def _audio_policy(plan: RenderPlan) -> AudioMixPolicy | None:
    properties = plan.output_profile.properties
    policy_value = properties.get("audio_policy")
    mastering_value = properties.get("audio_mastering")
    if not isinstance(policy_value, Mapping) or not isinstance(mastering_value, Mapping):
        return None
    try:
        return AudioMixPolicy(
            policy_id=str(policy_value.get("policy_id", "default")),
            version=str(policy_value.get("version", "1.0")),
            normalize=bool(mastering_value.get("normalize", False)),
            target_integrated_lufs=float(mastering_value.get("target_integrated_lufs", -14.0)),
            target_true_peak_dbfs=float(mastering_value.get("target_true_peak_dbfs", -1.0)),
            target_lra=float(mastering_value.get("target_lra", 11.0)),
            limiter_dbfs=float(mastering_value.get("limiter_dbfs", -1.0)),
        )
    except (TypeError, ValueError):
        return None


def _loudness_check(
    plan: RenderPlan,
    artifact: RenderArtifactManifest,
    output_path: Path,
    *,
    ffmpeg_path: str,
    timeout: float,
) -> QCCheckResult:
    if not artifact.has_audio:
        return QCCheckResult(
            name="audio_loudness",
            status="pass" if not plan.audio_tracks else "fail",
            message=(
                "artifact has no audio and no timeline audio requires loudness QC"
                if not plan.audio_tracks
                else "artifact is missing audio required by the timeline"
            ),
            details={},
        )
    policy = _audio_policy(plan)
    if policy is None:
        return QCCheckResult(
            name="audio_loudness",
            status="not_evaluable",
            blocking=False,
            message="artifact has audio but no frozen PR14 mastering policy to evaluate",
            details={},
        )
    command = compile_loudness_analysis_command(
        output_path,
        ffmpeg_path=ffmpeg_path,
        policy=policy,
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip()[-4096:] or "loudness analysis failed")
        measurement = parse_loudnorm_measurement(completed.stderr)
        result = evaluate_audio_qc(measurement, policy)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        return QCCheckResult(
            name="audio_loudness",
            status="not_evaluable",
            blocking=False,
            message="final artifact loudness analysis could not be evaluated",
            details={"error": str(exc)[:2048]},
        )
    return QCCheckResult(
        name="audio_loudness",
        status="pass" if result.passed else "fail",
        message=(
            "final artifact satisfies the frozen loudness/true-peak policy"
            if result.passed
            else "final artifact violates the frozen loudness/true-peak policy"
        ),
        details=result.model_dump(mode="json"),
    )


def run_render_qc(
    *,
    batch_job_id: str,
    item_key: str,
    plan: RenderPlan,
    artifact: RenderArtifactManifest,
    output_path: str | Path,
    ffmpeg_path: str = "ffmpeg",
    analysis_timeout: float = 60.0,
    maximum_black_ratio: float = 0.98,
) -> RenderQCReport:
    """Evaluate blocking structural QC plus best-effort visual/audio analyses."""

    path = Path(output_path)
    checks = [
        _dimension_check(plan, artifact),
        _duration_check(plan, artifact),
        _asset_check(plan, artifact),
        _audio_presence_check(plan, artifact),
        _overflow_check(plan),
        _safe_zone_check(plan),
    ]
    try:
        black = analyze_black_frames(
            path,
            duration_seconds=artifact.duration_seconds,
            ffmpeg_path=ffmpeg_path,
            timeout=analysis_timeout,
        )
        checks.append(
            QCCheckResult(
                name="black_frames",
                status="fail" if black.black_ratio >= maximum_black_ratio else "pass",
                message=(
                    "artifact is predominantly black"
                    if black.black_ratio >= maximum_black_ratio
                    else "artifact is not predominantly black"
                ),
                details=black.model_dump(mode="json"),
            )
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        checks.append(
            QCCheckResult(
                name="black_frames",
                status="not_evaluable",
                blocking=False,
                message="black-frame analysis could not be evaluated",
                details={"error": str(exc)[:2048]},
            )
        )
    checks.append(
        _loudness_check(
            plan,
            artifact,
            path,
            ffmpeg_path=ffmpeg_path,
            timeout=analysis_timeout,
        )
    )
    return RenderQCReport(
        batch_job_id=batch_job_id,
        item_key=item_key,
        render_job_id=artifact.job_id,
        project_id=artifact.project_id,
        checks=tuple(checks),
    )


__all__ = ["BlackFrameAnalysis", "analyze_black_frames", "run_render_qc"]
