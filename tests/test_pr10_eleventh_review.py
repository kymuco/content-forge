from __future__ import annotations

import pytest

from content_forge.application.review import ReviewConflictError
from content_forge.application.review_seventh_hardening import ReviewService
from content_forge.core import AttentionMode, Project, ProjectState
from content_forge.storage import LocalLibrary


def _manual_reentry_count(library: LocalLibrary) -> int:
    with library.database.connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE job_type = 'review_manual_reentry'"
        ).fetchone()
    assert row is not None
    return int(row["count"])


def test_unchanged_bootstrap_fast_path_still_validates_reserved_authority(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)
    project = library.save_project(Project(content_kind="note", state=ProjectState.INBOX))

    prepared = service.bootstrap_project(project.project_id)
    assert prepared.metadata["pr10_review_initialized"] is True
    assert prepared.metadata["review_renderable"] is False
    assert isinstance(
        prepared.metadata["pr10_manual_setup_input_fingerprint"],
        str,
    )
    assert _manual_reentry_count(library) == 0

    source_setup = next(
        task for task in prepared.review_tasks if task.task_type == "source_setup"
    )
    forged_source_setup = source_setup.validated_copy(
        update={"attention": AttentionMode.REVIEW}
    )
    corrupted = prepared.validated_copy(
        update={
            "review_tasks": tuple(
                forged_source_setup
                if task.review_task_id == source_setup.review_task_id
                else task
                for task in prepared.review_tasks
            )
        }
    )
    library.save_project(corrupted)

    with pytest.raises(ReviewConflictError, match="authority collision"):
        service.bootstrap_project(project.project_id)

    current = service.get_project(project.project_id)
    current_source_setup = next(
        task for task in current.review_tasks if task.task_type == "source_setup"
    )
    assert current_source_setup.attention is AttentionMode.REVIEW
    assert _manual_reentry_count(library) == 0
