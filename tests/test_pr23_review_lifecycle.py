from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.application import VoicedSceneConflictError
from content_forge.application.voiced_scene_hardening import VoicedSceneWorkflow
from content_forge.core import (
    AttentionMode,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
    ReviewTask,
)
import content_forge.application.voiced_scene as voiced_scene_base


def _approved_project(*, priority: ReviewPriority = ReviewPriority.BLOCKING) -> Project:
    project = Project(content_kind="pr23_review_fixture", state=ProjectState.READY)
    now = datetime.now(timezone.utc)
    preview = ReviewTask(
        project_id=project.project_id,
        task_type="preview_approval",
        status=ReviewStatus.RESOLVED,
        attention=AttentionMode.REVIEW,
        priority=priority,
        blocking=True,
        payload={
            "status": "ready",
            "job_id": "cf_job_preview",
            "render_plan_digest": "a" * 64,
            "project_revision_digest": "b" * 64,
        },
        accepted_value="cf_job_preview",
        created_at=now,
        resolved_at=now,
    )
    return project.validated_copy(
        update={
            "review_tasks": (preview,),
            "metadata": {
                "pr10_review_initialized": True,
                "review_renderable": True,
                "approved_preview_job_id": "cf_job_preview",
                "approved_preview_plan_digest": "a" * 64,
                "approved_preview_revision_digest": "b" * 64,
                "active_final_plan_digest": "c" * 64,
                "final_render_job_id": "cf_job_final",
                "final_render_plan_digest": "d" * 64,
                "final_output_sha256": "e" * 64,
                "unrelated": "preserve",
            },
        }
    )


def test_pr23_presentation_change_reopens_preview_and_clears_render_identity() -> None:
    updated = VoicedSceneWorkflow._invalidate_pr10_render_identity(_approved_project())

    assert updated.state is ProjectState.NEEDS_REVIEW
    preview = updated.review_tasks[0]
    assert preview.task_type == "preview_approval"
    assert preview.status is ReviewStatus.OPEN
    assert preview.accepted_value is None
    assert preview.resolved_at is None
    assert preview.payload == {"status": "not_rendered"}
    for key in (
        "approved_preview_job_id",
        "approved_preview_plan_digest",
        "approved_preview_revision_digest",
        "active_final_plan_digest",
        "final_render_job_id",
        "final_render_plan_digest",
        "final_output_sha256",
    ):
        assert key not in updated.metadata
    assert updated.metadata["unrelated"] == "preserve"


def test_pr23_reopened_project_remains_editable_for_presentation_iteration() -> None:
    assert ProjectState.NEEDS_REVIEW in voiced_scene_base._EDITABLE_STATES


def test_pr23_rejects_colliding_reserved_preview_authority() -> None:
    project = _approved_project(priority=ReviewPriority.HIGH)

    with pytest.raises(VoicedSceneConflictError, match="collides"):
        VoicedSceneWorkflow._invalidate_pr10_render_identity(project)


def test_pr23_non_pr10_project_keeps_its_lifecycle_untouched() -> None:
    project = Project(content_kind="standalone", state=ProjectState.READY)
    assert VoicedSceneWorkflow._invalidate_pr10_render_identity(project) == project
