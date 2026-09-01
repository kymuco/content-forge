from __future__ import annotations

import pytest

from content_forge.api import create_app
from content_forge.application.review import ReviewNotReadyError
from content_forge.application.voiced_story import (
    VoicedStoryConflictError,
    VoicedStoryValidationError,
    VoicedStoryWorkflow,
)
from content_forge.application.voiced_story_review import (
    VoicedStoryAwareReviewService,
    validate_materialized_voiced_story,
)
from content_forge.core import Project
from content_forge.storage import LocalLibrary


def test_pr22_review_routes_use_exact_snapshot_voiced_story_guard(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(root=tmp_path / "runtime")
    try:
        review = app.state.review
        assert isinstance(review, VoicedStoryAwareReviewService)
        project = Project(content_kind="panel_sequence")
        seen: list[Project] = []

        def reject_stale(_workflow, candidate: Project):
            seen.append(candidate)
            raise VoicedStoryConflictError("synthetic stale PR22 evidence")

        monkeypatch.setattr(
            "content_forge.application.voiced_story_review.validate_materialized_voiced_story",
            reject_stale,
        )

        with pytest.raises(
            ReviewNotReadyError,
            match="PR22 voiced-story materialization is stale: synthetic stale PR22 evidence",
        ):
            review._compile_plan(project, "unused-profile")  # noqa: SLF001
        assert seen == [project]
    finally:
        app.state.runtime_lease.close()


def test_pr22_render_guard_is_noop_without_materialization(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    workflow = VoicedStoryWorkflow(library)
    project = Project(content_kind="panel_sequence")

    assert validate_materialized_voiced_story(workflow, project) is None


def test_pr22_render_guard_rejects_malformed_materialization(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    workflow = VoicedStoryWorkflow(library)
    project = Project(
        content_kind="panel_sequence",
        metadata={"pr22_voiced_story": {"contract_version": "corrupt"}},
    )

    with pytest.raises(
        VoicedStoryValidationError,
        match="stored PR22 voiced story manifest is malformed",
    ):
        validate_materialized_voiced_story(workflow, project)
