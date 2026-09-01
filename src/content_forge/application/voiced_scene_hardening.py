"""Adversarial hardening for PR23 retained presentation ownership and review lifecycle.

The base PR23 workflow retains pre-presentation track/motion state so materialization is
reversible. This layer binds that retained state back to the exact derived plan before it
may ever be trusted for restoration. It also atomically invalidates an older PR10 preview
identity whenever PR23 changes render semantics, so final rendering can never be wedged
behind a stale already-resolved preview task.
"""

from __future__ import annotations

from content_forge.core import (
    AttentionMode,
    AudioTrack,
    MotionSpec,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
)

from . import voiced_scene as _base

# PR23 can be iterated or removed after it has deliberately reopened PR10 preview review.
# Upstream PR22 authority is still immutable here; only PR23-owned presentation changes.
_base._EDITABLE_STATES = frozenset(
    {
        ProjectState.DRAFT,
        ProjectState.PREPARED,
        ProjectState.NEEDS_REVIEW,
        ProjectState.READY,
    }
)

_RESERVED_TRACK_PROPERTY_KEYS = frozenset(
    {"pr23_owner", "pr23_preset_id", "pr23_preset_version"}
)
_PREVIEW_TASK_TYPE = "preview_approval"
_RENDER_IDENTITY_KEYS = (
    "approved_preview_job_id",
    "approved_preview_plan_digest",
    "approved_preview_revision_digest",
    "active_final_plan_digest",
    "final_render_job_id",
    "final_render_plan_digest",
    "final_output_sha256",
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
    """Bind reversible state to the plan and keep PR10 preview authority current."""

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

    @staticmethod
    def _invalidate_pr10_render_identity(project: Project) -> Project:
        """Reopen the exact PR10 preview task when PR23 changes render semantics."""

        if not bool(project.metadata.get("pr10_review_initialized")):
            return project

        preview_tasks = tuple(
            task for task in project.review_tasks if task.task_type == _PREVIEW_TASK_TYPE
        )
        if len(preview_tasks) != 1:
            if bool(project.metadata.get("review_renderable")) or any(
                key in project.metadata for key in _RENDER_IDENTITY_KEYS
            ):
                raise _base.VoicedSceneConflictError(
                    "PR10 preview authority is malformed while PR23 changes presentation"
                )
            return project

        preview = preview_tasks[0]
        if (
            preview.attention is not AttentionMode.REVIEW
            or preview.priority is not ReviewPriority.BLOCKING
            or preview.blocking is not True
        ):
            raise _base.VoicedSceneConflictError(
                "PR10 preview authority collides with reserved presentation lifecycle"
            )

        reopened = preview.validated_copy(
            update={
                "status": ReviewStatus.OPEN,
                "accepted_value": None,
                "resolved_at": None,
                "payload": {"status": "not_rendered"},
            }
        )
        tasks = tuple(
            reopened if task.review_task_id == preview.review_task_id else task
            for task in project.review_tasks
        )
        metadata = dict(project.metadata)
        for key in _RENDER_IDENTITY_KEYS:
            metadata.pop(key, None)

        state = project.state
        if state in {ProjectState.READY, ProjectState.NEEDS_REVIEW}:
            state = ProjectState.NEEDS_REVIEW
        return project.validated_copy(
            update={
                "state": state,
                "review_tasks": tasks,
                "metadata": metadata,
            }
        )

    def _base_project(
        self,
        project: Project,
        manifest: _base.ProjectVoicedSceneManifest | None,
    ) -> Project:
        if manifest is not None:
            self._validate_retained_ownership(manifest)
        return super()._base_project(project, manifest)

    def validate_snapshot(self, project: Project) -> _base.ProjectVoicedSceneManifest:
        """Validate current PR22/PR23 authority using exactly the supplied Project object.

        Callers that already own a Project snapshot should use this method instead of
        ``manifest(project_id)`` so validation and subsequent composition cannot observe
        different database revisions.
        """

        stored = _base.voiced_scene_manifest(project)
        if stored is None:
            raise _base.VoicedSceneNotFoundError(
                "project has no materialized PR23 voiced-scene presentation"
            )
        base = self._base_project(project, stored)
        expected = self.derive(base, preset=stored.plan.preset)
        if expected != stored.plan:
            raise _base.VoicedSceneConflictError(
                "materialized PR23 plan no longer matches current PR22/project authority"
            )
        return stored

    def _cas_project(self, expected_json: str, updated: Project) -> Project:
        # Presentation mutation and preview invalidation are one CAS. There is never an
        # intermediate persisted READY snapshot whose approved preview describes the old
        # camera/mix state while PR23 already describes the new render semantics.
        return super()._cas_project(
            expected_json,
            self._invalidate_pr10_render_identity(updated),
        )


__all__ = ["VoicedSceneWorkflow"]
