"""PR23 render-authority gate for voiced projects.

PR10 remains the render/review lifecycle owner. This module installs one exact-snapshot
voiced-presentation guard onto the existing seventh-pass ReviewService class *in place*.
Keeping the class object unchanged preserves the historical public identity contract while
still making every existing preview/final compile path fail closed for stale PR23 state.
"""

from __future__ import annotations

from content_forge.core import Project

from .dialogue import DialogueError
from .review import ReviewNotReadyError
from .review_seventh_hardening import ReviewService as _BaseReviewService
from .voiced_scene import VoicedSceneError, voiced_scene_manifest
from .voiced_scene_hardening import VoicedSceneWorkflow
from .voiced_story import VoicedStoryError, voiced_story_manifest


_BASE_COMPILE_PLAN = _BaseReviewService._compile_plan


def _require_pr23_render_authority(self: _BaseReviewService, project: Project) -> None:
    try:
        pr22 = voiced_story_manifest(project)
    except VoicedStoryError as exc:
        raise ReviewNotReadyError(
            f"materialized PR22 voiced-story authority is invalid: {exc}"
        ) from exc
    if pr22 is None:
        return

    try:
        stored = voiced_scene_manifest(project)
        if stored is None:
            raise ReviewNotReadyError(
                "materialized PR22 voiced story requires PR23 presentation before render"
            )
        workflow = VoicedSceneWorkflow(self.library)
        # Validate from the exact Project object supplied by PR10. Do not call
        # workflow.manifest(project_id), which would take a second database snapshot and
        # introduce a TOCTOU gap between review claim validation and compilation.
        base = workflow._base_project(project, stored)
        expected = workflow.derive(base, preset=stored.plan.preset)
        if expected != stored.plan:
            raise ReviewNotReadyError(
                "materialized PR23 presentation is stale for current voiced project"
            )
    except ReviewNotReadyError:
        raise
    except (VoicedSceneError, DialogueError, VoicedStoryError) as exc:
        raise ReviewNotReadyError(
            f"materialized PR23 presentation authority is invalid: {exc}"
        ) from exc


def _compile_plan(self: _BaseReviewService, project: Project, profile_id: str):
    _require_pr23_render_authority(self, project)
    return _BASE_COMPILE_PLAN(self, project, profile_id)


# Preserve the public class identity established by PR10's seventh hardening pass. PR17
# already uses the same in-place installation pattern for startup authority. The marker
# makes accidental module reloads idempotent instead of stacking guards recursively.
setattr(_compile_plan, "_content_forge_pr23_guard", True)
if not getattr(_BaseReviewService._compile_plan, "_content_forge_pr23_guard", False):
    setattr(_BaseReviewService, "_require_pr23_render_authority", _require_pr23_render_authority)
    setattr(_BaseReviewService, "_compile_plan", _compile_plan)

ReviewService = _BaseReviewService


__all__ = ["ReviewService"]
