"""PR14 renderer-independent audio composition and mastering contracts."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from content_forge.core.models import FrozenModel


class AudioMixPolicy(FrozenModel):
    """Explicit per-template/project audio policy.

    The policy is applied upstream to canonical AudioTrack properties and output-profile
    mastering evidence. The renderer never invents policy from content type.
    """

    policy_id: str = Field(default="default", min_length=1, max_length=128)
    version: str = Field(default="1.0", min_length=1, max_length=64)
    original_gain_db: float = Field(default=0.0, ge=-60.0, le=24.0)
    music_gain_db: float = Field(default=-12.0, ge=-60.0, le=24.0)
    music_duck_db: float = Field(default=-8.0, ge=-60.0, le=0.0)
    fade_in_seconds: float = Field(default=0.0, ge=0.0, le=30.0)
    fade_out_seconds: float = Field(default=0.0, ge=0.0, le=30.0)
    normalize: bool = False
    target_integrated_lufs: float = Field(default=-14.0, ge=-36.0, le=-5.0)
    target_true_peak_dbfs: float = Field(default=-1.0, ge=-9.0, le=0.0)
    target_lra: float = Field(default=11.0, ge=1.0, le=20.0)
    limiter_dbfs: float = Field(default=-1.0, ge=-12.0, le=0.0)


class LoudnessMeasurement(FrozenModel):
    """Values emitted by FFmpeg loudnorm's measurement pass.

    FFmpeg reports silence with non-finite sentinels (`-inf` for input loudness/peak and
    `inf` for target offset). Canonical Content Forge models disallow NaN/Inf globally,
    so PR14 represents those explicit loudnorm sentinels as `None`. Such measurements
    remain valid QC evidence but are deliberately not usable for the normalization pass.
    """

    input_i: float | None
    input_tp: float | None
    input_lra: float = Field(ge=0.0)
    input_thresh: float | None
    target_offset: float | None

    @model_validator(mode="after")
    def validate_silence_shape(self) -> Self:
        if (self.input_i is None) != (self.input_tp is None):
            raise ValueError(
                "loudness measurement input_i/input_tp must both be finite or both be silence sentinels"
            )
        return self

    @property
    def silent_sentinel(self) -> bool:
        return self.input_i is None and self.input_tp is None

    @property
    def normalizable(self) -> bool:
        return all(
            value is not None
            for value in (
                self.input_i,
                self.input_tp,
                self.input_thresh,
                self.target_offset,
            )
        )


class AudioQCResult(FrozenModel):
    integrated_lufs: float | None
    true_peak_dbfs: float | None
    loudness_range_lu: float = Field(ge=0.0)
    silent: bool
    loudness_ok: bool
    true_peak_ok: bool

    @property
    def passed(self) -> bool:
        return not self.silent and self.loudness_ok and self.true_peak_ok


__all__ = ["AudioMixPolicy", "AudioQCResult", "LoudnessMeasurement"]
