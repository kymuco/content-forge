"""PR22 fail-closed guard for the PR10 preview/final render path."""

from __future__ import annotations

from content_forge.core import Project
from content_forge.timeline import RenderPlan

from .dialogue import DialogueError
from .review import ReviewNotReadyError, ReviewService
from .tts import TTSError
from .voice_cast import VoiceCastError
from .voiced_story import (
    ProjectVoicedStoryManifest,
    VoicedStoryConflictError,
    VoicedStoryError,
    VoicedStoryWorkflow,
    _is_pr22_timed_text,
    _is_pr22_voice_audio,
    _scene_materialization_matches,
    voiced_story_manifest,
)


def validate_materialized_voiced_story(
    workflow: VoicedStoryWorkflow,
    project: Project,
) -> ProjectVoicedStoryManifest | None:
    """Validate PR22 against the exact Project snapshot about to be rendered."""

    stored = voiced_story_manifest(project)
    if stored is None:
        orphaned = any(
            any(_is_pr22_timed_text(overlay) for overlay in scene.overlays)
            or any(_is_pr22_voice_audio(track) for track in scene.audio_tracks)
            for scene in project.scenes
        )
        if orphaned:
            raise VoicedStoryConflictError(
                "PR22-owned scene state exists without a materialization manifest"
            )
        return None
    expected = workflow.derive(project, policy=stored.timing_policy)
    if stored != expected or not _scene_materialization_matches(project, stored):
        raise VoicedStoryConflictError(
            "materialized PR22 voiced story no longer matches current upstream authority"
        )
    return stored


class VoicedStoryAwareReviewService(ReviewService):
    """PR10 review service that refuses stale PR22 materialization before compilation."""

    def _compile_plan(self, project: Project, profile_id: str) -> RenderPlan:
        try:
            validate_materialized_voiced_story(VoicedStoryWorkflow(self.library), project)
        except (VoicedStoryError, DialogueError, TTSError, VoiceCastError) as exc:
            raise ReviewNotReadyError(
                f"PR22 voiced-story materialization is stale: {exc}"
            ) from exc
        return super()._compile_plan(project, profile_id)


__all__ = [
    "VoicedStoryAwareReviewService",
    "validate_materialized_voiced_story",
]
