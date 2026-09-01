from __future__ import annotations

import pytest

from content_forge.application import (
    ProjectVoicedSceneManifest,
    ProjectVoicedScenePlan,
    VoicedSceneConflictError,
    VoicedSceneScenePlan,
    VoicedSceneTrackPlan,
)
from content_forge.application.voiced_scene import (
    VoicedSceneOwnedMotion,
    VoicedSceneOwnedTrack,
)
from content_forge.application.voiced_scene_hardening import VoicedSceneWorkflow
from content_forge.core import (
    AudioTrack,
    EntityKind,
    MotionSpec,
    NormalizedPoint,
    new_entity_id,
)


def _manifest() -> ProjectVoicedSceneManifest:
    project_id = new_entity_id(EntityKind.PROJECT)
    scene_id = new_entity_id(EntityKind.SCENE)
    base_track = AudioTrack(
        track_type="music",
        gain_db=-8.0,
        properties={"existing_policy": "keep", "duck_db": -2.0},
    )
    track_plan = VoicedSceneTrackPlan(
        audio_track_id=base_track.audio_track_id,
        track_type="music",
        duck_db=-10.0,
    )
    materialized_track = base_track.validated_copy(
        update={
            "properties": {
                "existing_policy": "keep",
                "duck_db": -10.0,
                "pr23_owner": "pr23_voiced_mix_v1",
                "pr23_preset_id": "natural_dialogue",
                "pr23_preset_version": "1",
            }
        }
    )
    base_motion = MotionSpec(
        motion_type="slow_zoom",
        focus=NormalizedPoint(x=0.5, y=0.5),
        properties={"legacy": True},
    )
    materialized_motion = MotionSpec(
        motion_type="focus_zoom",
        focus=NormalizedPoint(x=0.62, y=0.42),
        properties={
            "pr23_owner": "pr23_camera_v1",
            "preset_id": "natural_dialogue",
            "preset_version": "1",
            "focus_source": "face_hint",
            "start_scale": 0.86,
            "end_scale": 0.74,
        },
    )
    scene_plan = VoicedSceneScenePlan(
        scene_id=scene_id,
        pr22_scene_sha256="b" * 64,
        camera_action="focus_zoom",
        camera_source="face_hint",
        proposed_motion=materialized_motion,
    )
    plan = ProjectVoicedScenePlan(
        project_id=project_id,
        pr22_manifest_sha256="a" * 64,
        scenes=(scene_plan,),
        tracks=(track_plan,),
    )
    return ProjectVoicedSceneManifest(
        project_id=project_id,
        plan=plan,
        owned_tracks=(
            VoicedSceneOwnedTrack(
                base_track=base_track,
                materialized_track=materialized_track,
            ),
        ),
        owned_motions=(
            VoicedSceneOwnedMotion(
                scene_id=scene_id,
                base_motion=base_motion,
                materialized_motion=materialized_motion,
            ),
        ),
    )


def test_pr23_retained_ownership_accepts_exact_plan_transform() -> None:
    VoicedSceneWorkflow._validate_retained_ownership(_manifest())


def test_pr23_retained_track_cannot_restore_fabricated_base_semantics() -> None:
    manifest = _manifest()
    owned = manifest.owned_tracks[0]
    fabricated_base = owned.base_track.validated_copy(update={"gain_db": -30.0})
    tampered = manifest.validated_copy(
        update={
            "owned_tracks": (
                owned.validated_copy(update={"base_track": fabricated_base}),
            )
        }
    )

    with pytest.raises(VoicedSceneConflictError, match="non-presentation track semantics"):
        VoicedSceneWorkflow._validate_retained_ownership(tampered)


def test_pr23_retained_track_must_match_exact_duck_transform() -> None:
    manifest = _manifest()
    owned = manifest.owned_tracks[0]
    properties = dict(owned.materialized_track.properties)
    properties["duck_db"] = -9.0
    tampered_track = owned.materialized_track.validated_copy(update={"properties": properties})
    tampered = manifest.validated_copy(
        update={
            "owned_tracks": (
                owned.validated_copy(update={"materialized_track": tampered_track}),
            )
        }
    )

    with pytest.raises(VoicedSceneConflictError, match="exact planned transform"):
        VoicedSceneWorkflow._validate_retained_ownership(tampered)


def test_pr23_retained_motion_must_equal_planned_motion() -> None:
    manifest = _manifest()
    owned = manifest.owned_motions[0]
    tampered_motion = owned.materialized_motion.validated_copy(
        update={"focus": NormalizedPoint(x=0.1, y=0.1)}
    )
    tampered = manifest.validated_copy(
        update={
            "owned_motions": (
                owned.validated_copy(update={"materialized_motion": tampered_motion}),
            )
        }
    )

    with pytest.raises(VoicedSceneConflictError, match="exact planned motion"):
        VoicedSceneWorkflow._validate_retained_ownership(tampered)


def test_pr23_retained_ownership_cannot_contain_extra_track() -> None:
    manifest = _manifest()
    extra_base = AudioTrack(track_type="ambience")
    extra_materialized = extra_base.validated_copy(
        update={
            "properties": {
                "pr23_owner": "pr23_voiced_mix_v1",
                "pr23_preset_id": "natural_dialogue",
                "pr23_preset_version": "1",
                "duck_db": -6.0,
            }
        }
    )
    tampered = manifest.validated_copy(
        update={
            "owned_tracks": manifest.owned_tracks
            + (
                VoicedSceneOwnedTrack(
                    base_track=extra_base,
                    materialized_track=extra_materialized,
                ),
            )
        }
    )

    with pytest.raises(VoicedSceneConflictError, match="exactly match"):
        VoicedSceneWorkflow._validate_retained_ownership(tampered)
