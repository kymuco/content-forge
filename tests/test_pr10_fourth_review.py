from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.application.review import ReviewConflictError, ReviewService
from content_forge.core import (
    Asset,
    AssetRef,
    AttentionMode,
    MediaType,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
    ReviewTask,
)
from content_forge.storage import LocalLibrary


def _task(project: Project, task_type: str) -> ReviewTask:
    return next(task for task in project.review_tasks if task.task_type == task_type)


def _image_asset(library: LocalLibrary, seed: int) -> Asset:
    return library.database.put_asset(
        Asset(
            sha256=f"{seed:064x}",
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=100 + seed,
            width=1080,
            height=1920,
        )
    )


def test_uninitialized_bootstrap_rejects_matching_reserved_task_lifecycle(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = Project(content_kind="image", state=ProjectState.INBOX)
    now = datetime.now(timezone.utc)
    preaccepted_crop = ReviewTask(
        project_id=project.project_id,
        task_type="crop_confirmation",
        status=ReviewStatus.RESOLVED,
        attention=AttentionMode.REVIEW,
        priority=ReviewPriority.HIGH,
        blocking=True,
        payload={"scene_ids": [], "crops": {}},
        accepted_value={"crops": {}},
        resolved_at=now,
        created_at=now,
    )
    project = library.save_project(
        project.validated_copy(update={"review_tasks": (preaccepted_crop,)})
    )
    service = ReviewService(library)

    with pytest.raises(
        ReviewConflictError,
        match="uninitialized project already contains reserved review task",
    ):
        service.bootstrap_project(project.project_id)

    current = service.get_project(project.project_id)
    assert current.state is ProjectState.INBOX
    assert current.review_tasks == (preaccepted_crop,)
    assert not current.metadata.get("pr10_review_initialized")


def test_restart_reconciliation_validates_facade_authority_before_mutation(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    asset = _image_asset(library, 1501)
    project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )
    service = ReviewService(library)
    initialized = service.bootstrap_project(project.project_id)
    preview = _task(initialized, "preview_approval")

    foreign_preview = preview.validated_copy(
        update={
            "attention": AttentionMode.MANUAL,
            "payload": {
                "status": "rendering",
                "claim_id": "foreign-claim",
                "render_plan_digest": "f" * 64,
                "project_revision_digest": "e" * 64,
            },
        }
    )
    corrupted = initialized.validated_copy(
        update={
            "review_tasks": tuple(
                foreign_preview if task.review_task_id == preview.review_task_id else task
                for task in initialized.review_tasks
            )
        }
    )
    library.save_project(corrupted)

    with pytest.raises(
        ReviewConflictError,
        match="reserved review task authority collision: preview_approval",
    ):
        service.reconcile_persisted_state()

    # Inherited recovery would reset a persisted preview `rendering` claim. The facade
    # must fail before that or any other recovery mutation occurs.
    current = service.get_project(project.project_id)
    current_preview = _task(current, "preview_approval")
    assert current_preview.attention is AttentionMode.MANUAL
    assert current_preview.payload["status"] == "rendering"
    assert current_preview.payload["claim_id"] == "foreign-claim"
