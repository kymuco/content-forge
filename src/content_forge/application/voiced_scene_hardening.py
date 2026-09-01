"""Adversarial hardening for PR23 retained presentation ownership.

The base PR23 workflow retains pre-presentation track/motion state so materialization is
reversible. This layer binds that retained state back to the exact derived plan before it
may ever be trusted for restoration. Corrupt or fabricated metadata therefore cannot use
PR23 dematerialization as an arbitrary Project mutation primitive.
"""

from __future__ import annotations

from content_forge.core import AudioTrack, MotionSpec, Project

from . import voiced_scene as _base

_RESERVED_TRACK_PROPERTY_KEYS = frozenset(
    {"pr23_owner", "pr23_preset_id", "pr23_preset_version"}
)


def _track_semantics_without_properties(track: AudioTrack) -> dict[str, object]:
    payload = track.model_dump(mode="json")
    payload.pop("properties", None)
    return payload


def _expected_materialized_track_properties(
    base_track: AudioTrack,
    *,
    plan: _base.VoicedSceneTrackPlan,
    preset: _base.VoicedScenePreset,
) -> dict[str, object]:
    properties = dict(base_track.properties)
    properties["pr23_owner"] = _base._MIX_OWNER
    properties["pr23_preset_id"] = preset.preset_id
    properties["pr23_preset_version"] = preset.version
    properties["duck_db"] = plan.duck_db
    return properties


def _reserved_track_namespace_is_clean(track: AudioTrack) -> bool:
    return not any(key in track.properties for key in _RESERVED_TRACK_PROPERTY_KEYS)


def _reserved_motion_namespace_is_clean(motion: MotionSpec | None) -> bool:
    return motion is None or "pr23_owner" not in motion.properties


class VoicedSceneWorkflow(_base.VoicedSceneWorkflow):
    """Bind retained reversible state exactly to the current PR23 presentation plan."""

    @staticmethod
    def _validate_retained_ownership(
        manifest: _base.ProjectVoicedSceneManifest,
    ) -> None:
        track_plan = {
            (item.scope_scene_id, item.audio_track_id): item
            for item in manifest.plan.tracks
        }
        owned_tracks = {
            (item.scope_scene_id, item.base_track.audio_track_id): item
            for item in manifest.owned_tracks
        }
        if set(owned_tracks) != set(track_plan):
            raise _base.VoicedSceneConflictError(
                "PR23 retained audio ownership does not exactly match the presentation plan"
            )

        for key, plan in track_plan.items():
            owned = owned_tracks[key]
            base_track = owned.base_track
            materialized_track = owned.materialized_track
            if (
                base_track.track_type != plan.track_type
                or materialized_track.track_type != plan.track_type
            ):
                raise _base.VoicedSceneConflictError(
                    "PR23 retained audio ownership changed planned track type"
                )
            if not _reserved_track_namespace_is_clean(base_track):
                raise _base.VoicedSceneConflictError(
                    "PR23 retained base track already occupies the reserved PR23 namespace"
                )
            if _track_semantics_without_properties(base_track) != _track_semantics_without_properties(
                materialized_track
            ):
                raise _base.VoicedSceneConflictError(
                    "PR23 retained audio ownership changed non-presentation track semantics"
                )
            expected_properties = _expected_materialized_track_properties(
                base_track,
                plan=plan,
                preset=manifest.plan.preset,
            )
            if materialized_track.properties != expected_properties:
                raise _base.VoicedSceneConflictError(
                    "PR23 retained audio materialization does not match the exact planned transform"
                )

        motion_plan = {
            item.scene_id: item.proposed_motion
            for item in manifest.plan.scenes
            if item.camera_action == "focus_zoom" and item.proposed_motion is not None
        }
        owned_motions = {item.scene_id: item for item in manifest.owned_motions}
        if set(owned_motions) != set(motion_plan):
            raise _base.VoicedSceneConflictError(
                "PR23 retained camera ownership does not exactly match the presentation plan"
            )
        for scene_id, proposed_motion in motion_plan.items():
            owned = owned_motions[scene_id]
            if not _reserved_motion_namespace_is_clean(owned.base_motion):
                raise _base.VoicedSceneConflictError(
                    "PR23 retained base motion already occupies the reserved PR23 namespace"
                )
            if owned.materialized_motion != proposed_motion:
                raise _base.VoicedSceneConflictError(
                    "PR23 retained camera materialization does not match the exact planned motion"
                )

    def _base_project(
        self,
        project: Project,
        manifest: _base.ProjectVoicedSceneManifest | None,
    ) -> Project:
        if manifest is not None:
            self._validate_retained_ownership(manifest)
        return super()._base_project(project, manifest)


__all__ = ["VoicedSceneWorkflow"]
