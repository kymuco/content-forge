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
    """Values emitted by FFmpeg loudnorm's measurement pass."""

    input_i: float
    input_tp: float
    input_lra: float = Field(ge=0.0)
    input_thresh: float
    target_offset: float

    @model_validator(mode="after")
    def finite_values(self) -> Self:
        values = (
            self.input_i,
            self.input_tp,
            self.input_lra,
            self.input_thresh,
            self.target_offset,
        )
        if any(value != value or value in {float("inf"), float("-inf")} for value in values):
            raise ValueError("loudness measurement values must be finite")
        return self


class AudioQCResult(FrozenModel):
    integrated_lufs: float
    true_peak_dbfs: float
    loudness_range_lu: float = Field(ge=0.0)
    silent: bool
    loudness_ok: bool
    true_peak_ok: bool

    @property
    def passed(self) -> bool:
        return not self.silent and self.loudness_ok and self.true_peak_ok


__all__ = ["AudioMixPolicy", "AudioQCResult", "LoudnessMeasurement"]
