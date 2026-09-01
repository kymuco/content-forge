"""PR24 exact-snapshot render gate for cross-project shared voiced scenes."""

from __future__ import annotations

from content_forge.core import Project

from .long_form_shared import LongFormSharedSceneError
from .long_form_shared_integrity import LongFormSharedSceneWorkflow
from .review import ReviewNotReadyError
from .review_pr23_hardening import ReviewService as _BaseReviewService


_BASE_COMPILE_PLAN = _BaseReviewService._compile_plan


def _require_pr24_shared_authority(
    self: _BaseReviewService,
    project: Project,
) -> None:
    try:
        # Validate exactly the Project snapshot already selected by PR10. Source projects
        # are resolved once each inside PR24's shared-scene workflow and pinned back to
        # the host-owned copy before the existing timeline compiler sees the project.
        LongFormSharedSceneWorkflow(self.library).validate_snapshot(project)
    except LongFormSharedSceneError as exc:
        raise ReviewNotReadyError(
            f"PR24 shared voiced-scene authority is invalid: {exc}"
        ) from exc


def _compile_plan(self: _BaseReviewService, project: Project, profile_id: str):
    _require_pr24_shared_authority(self, project)
    return _BASE_COMPILE_PLAN(self, project, profile_id)


setattr(_compile_plan, "_content_forge_pr24_guard", True)
if not getattr(_BaseReviewService._compile_plan, "_content_forge_pr24_guard", False):
    setattr(
        _BaseReviewService,
        "_require_pr24_shared_authority",
        _require_pr24_shared_authority,
    )
    setattr(_BaseReviewService, "_compile_plan", _compile_plan)

ReviewService = _BaseReviewService


__all__ = ["ReviewService"]
