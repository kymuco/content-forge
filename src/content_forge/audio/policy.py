"""PR14 helpers that materialize explicit audio policy into canonical project state."""

from __future__ import annotations

import math

from content_forge.core import AssetRef, AudioTrack, OutputProfile, Project, Scene

from .models import AudioMixPolicy, LoudnessMeasurement


def music_track(
    asset_ref: AssetRef,
    *,
    duration_seconds: float,
    start_seconds: float = 0.0,
    gain_db: float = 0.0,
    loop: bool = True,
    fade_in_seconds: float | None = None,
    fade_out_seconds: float | None = None,
    duck_db: float | None = None,
) -> AudioTrack:
    properties: dict[str, object] = {"base_gain_db": gain_db}
    if fade_in_seconds is not None:
        properties["base_fade_in_seconds"] = fade_in_seconds
        properties["fade_in_seconds"] = fade_in_seconds
    if fade_out_seconds is not None:
        properties["base_fade_out_seconds"] = fade_out_seconds
        properties["fade_out_seconds"] = fade_out_seconds
    if duck_db is not None:
        properties["base_duck_db"] = duck_db
        properties["duck_db"] = duck_db
    return AudioTrack(
        track_type="music",
        asset_ref=asset_ref,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        gain_db=gain_db,
        loop=loop,
        properties=properties,
    )


def original_audio_track(
    asset_ref: AssetRef,
    *,
    duration_seconds: float,
    start_seconds: float = 0.0,
    gain_db: float = 0.0,
    fade_in_seconds: float | None = None,
    fade_out_seconds: float | None = None,
) -> AudioTrack:
    properties: dict[str, object] = {"base_gain_db": gain_db}
    if fade_in_seconds is not None:
        properties["base_fade_in_seconds"] = fade_in_seconds
        properties["fade_in_seconds"] = fade_in_seconds
    if fade_out_seconds is not None:
        properties["base_fade_out_seconds"] = fade_out_seconds
        properties["fade_out_seconds"] = fade_out_seconds
    return AudioTrack(
        track_type="original",
        asset_ref=asset_ref,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        gain_db=gain_db,
        properties=properties,
    )


def _bounded_number(
    value: object,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"audio {name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"audio {name} is outside supported bounds")
    return result


def _base_gain_db(track: AudioTrack, properties: dict[str, object]) -> float:
    value = _bounded_number(
        properties.get("base_gain_db", track.gain_db),
        name="base_gain_db",
        minimum=-120.0,
        maximum=24.0,
    )
    properties["base_gain_db"] = value
    return value


def _explicit_or_policy_value(
    properties: dict[str, object],
    *,
    base_key: str,
    effective_key: str,
    policy_value: float,
    minimum: float,
    maximum: float,
) -> float:
    if base_key in properties:
        return _bounded_number(
            properties[base_key],
            name=base_key,
            minimum=minimum,
            maximum=maximum,
        )

    # Preserve a pre-PR14/manual track property as an explicit override the first time
    # policy is applied. On a track already materialized by policy, absence of base_key
    # means the effective value is policy-owned and may be replaced by a new policy.
    if "audio_policy_id" not in properties and effective_key in properties:
        explicit = _bounded_number(
            properties[effective_key],
            name=effective_key,
            minimum=minimum,
            maximum=maximum,
        )
        properties[base_key] = explicit
        return explicit
    return policy_value


def _policy_track(track: AudioTrack, policy: AudioMixPolicy) -> AudioTrack:
    properties = track.model_dump(mode="json")["properties"]
    base_gain = _base_gain_db(track, properties)

    properties["fade_in_seconds"] = _explicit_or_policy_value(
        properties,
        base_key="base_fade_in_seconds",
        effective_key="fade_in_seconds",
        policy_value=policy.fade_in_seconds,
        minimum=0.0,
        maximum=30.0,
    )
    properties["fade_out_seconds"] = _explicit_or_policy_value(
        properties,
        base_key="base_fade_out_seconds",
        effective_key="fade_out_seconds",
        policy_value=policy.fade_out_seconds,
        minimum=0.0,
        maximum=30.0,
    )

    gain_delta = 0.0
    if track.track_type == "music":
        gain_delta = policy.music_gain_db
        properties["duck_db"] = _explicit_or_policy_value(
            properties,
            base_key="base_duck_db",
            effective_key="duck_db",
            policy_value=policy.music_duck_db,
            minimum=-60.0,
            maximum=0.0,
        )
    elif track.track_type == "original":
        gain_delta = policy.original_gain_db

    properties["audio_policy_id"] = policy.policy_id
    properties["audio_policy_version"] = policy.version
    return track.validated_copy(
        update={"gain_db": base_gain + gain_delta, "properties": properties}
    )


def _policy_scene(scene: Scene, policy: AudioMixPolicy) -> Scene:
    return scene.validated_copy(
        update={
            "audio_tracks": tuple(
                _policy_track(track, policy) for track in scene.audio_tracks
            )
        }
    )


def _mastering_properties(
    profile: OutputProfile,
    policy: AudioMixPolicy,
    measurement: LoudnessMeasurement | None,
) -> OutputProfile:
    properties = profile.model_dump(mode="json")["properties"]
    properties["audio_policy"] = {
        "policy_id": policy.policy_id,
        "version": policy.version,
    }
    master: dict[str, object] = {
        "normalize": policy.normalize,
        "target_integrated_lufs": policy.target_integrated_lufs,
        "target_true_peak_dbfs": policy.target_true_peak_dbfs,
        "target_lra": policy.target_lra,
        "limiter_dbfs": policy.limiter_dbfs,
    }
    if measurement is not None:
        master["measurement"] = measurement.model_dump(mode="json")
    properties["audio_mastering"] = master
    return profile.validated_copy(update={"properties": properties})


def apply_audio_policy(
    project: Project,
    policy: AudioMixPolicy,
    *,
    measurements: dict[str, LoudnessMeasurement] | None = None,
) -> Project:
    """Materialize policy before timeline compilation.

    Templates may call this helper, but the core timeline/renderers remain unaware of
    content kind. Loudness normalization fails closed later if requested without frozen
    first-pass evidence for the selected output profile. Explicit track overrides are
    stored as base values, while policy-owned values can be safely replaced or reapplied.
    """

    by_profile = measurements or {}
    return project.validated_copy(
        update={
            "audio_tracks": tuple(
                _policy_track(track, policy) for track in project.audio_tracks
            ),
            "scenes": tuple(_policy_scene(scene, policy) for scene in project.scenes),
            "output_profiles": tuple(
                _mastering_properties(
                    profile,
                    policy,
                    by_profile.get(profile.profile_id),
                )
                for profile in project.output_profiles
            ),
        }
    )


__all__ = ["apply_audio_policy", "music_track", "original_audio_track"]
