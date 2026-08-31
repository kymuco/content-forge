"""PR14 helpers that materialize explicit audio policy into canonical project state."""

from __future__ import annotations

from content_forge.core import AssetRef, AudioTrack, OutputProfile, Project, Scene

from .models import AudioMixPolicy, LoudnessMeasurement


def music_track(
    asset_ref: AssetRef,
    *,
    duration_seconds: float,
    start_seconds: float = 0.0,
    gain_db: float = 0.0,
    loop: bool = True,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
    duck_db: float | None = None,
) -> AudioTrack:
    properties: dict[str, object] = {
        "fade_in_seconds": fade_in_seconds,
        "fade_out_seconds": fade_out_seconds,
    }
    if duck_db is not None:
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
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
) -> AudioTrack:
    return AudioTrack(
        track_type="original",
        asset_ref=asset_ref,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        gain_db=gain_db,
        properties={
            "fade_in_seconds": fade_in_seconds,
            "fade_out_seconds": fade_out_seconds,
        },
    )


def _policy_track(track: AudioTrack, policy: AudioMixPolicy) -> AudioTrack:
    properties = dict(track.properties)
    properties.setdefault("fade_in_seconds", policy.fade_in_seconds)
    properties.setdefault("fade_out_seconds", policy.fade_out_seconds)
    gain_delta = 0.0
    if track.track_type == "music":
        gain_delta = policy.music_gain_db
        properties.setdefault("duck_db", policy.music_duck_db)
    elif track.track_type == "original":
        gain_delta = policy.original_gain_db
    properties["audio_policy_id"] = policy.policy_id
    properties["audio_policy_version"] = policy.version
    return track.validated_copy(
        update={"gain_db": track.gain_db + gain_delta, "properties": properties}
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
    properties = dict(profile.properties)
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
    first-pass evidence for the selected output profile.
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
