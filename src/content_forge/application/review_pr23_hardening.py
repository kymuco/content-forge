"""PR23 render-authority gate for voiced projects.

PR10 remains the render/review lifecycle owner. This layer only prevents that lifecycle
from compiling a Project which already claims materialized PR22 voiced-story authority
unless the exact same Project snapshot also carries a current, reproducible PR23
presentation manifest.
"""

from __future__ import annotations

from content_forge.core import Project

from .dialogue import DialogueError
from .review import ReviewNotReadyError
from .review_seventh_hardening import ReviewService as _BaseReviewService
from .voiced_scene import VoicedSceneError, voiced_scene_manifest
from .voiced_scene_hardening import VoicedSceneWorkflow
from .voiced_story import VoicedStoryError, voiced_story_manifest


class ReviewService(_BaseReviewService):
    """Require current PR23 presentation before rendering materialized PR22 projects."""

    def _require_pr23_render_authority(self, project: Project) -> None:
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
            # workflow.manifest(project_id), which would take a second database snapshot
            # and introduce a TOCTOU gap between review claim validation and compilation.
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

    def _compile_plan(self, project: Project, profile_id: str):
        self._require_pr23_render_authority(project)
        return super()._compile_plan(project, profile_id)


__all__ = ["ReviewService"]
