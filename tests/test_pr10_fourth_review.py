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


def test_restart_reconciliation_quarantines_invalid_authority_and_recovers_valid(
    tmp_path,
) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)

    bad_asset = _image_asset(library, 1501)
    bad_project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=bad_asset.asset_id),),
        )
    )
    bad_initialized = service.bootstrap_project(bad_project.project_id)
    bad_preview = _task(bad_initialized, "preview_approval")
    foreign_preview = bad_preview.validated_copy(
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
    library.save_project(
        bad_initialized.validated_copy(
            update={
                "review_tasks": tuple(
                    foreign_preview
                    if task.review_task_id == bad_preview.review_task_id
                    else task
                    for task in bad_initialized.review_tasks
                )
            }
        )
    )

    good_asset = _image_asset(library, 1502)
    good_project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=good_asset.asset_id),),
        )
    )
    good_initialized = service.bootstrap_project(good_project.project_id)
    good_preview = _task(good_initialized, "preview_approval")
    rendering_preview = good_preview.validated_copy(
        update={
            "payload": {
                "status": "rendering",
                "claim_id": "valid-claim",
                "render_plan_digest": "a" * 64,
                "project_revision_digest": "b" * 64,
            }
        }
    )
    library.save_project(
        good_initialized.validated_copy(
            update={
                "review_tasks": tuple(
                    rendering_preview
                    if task.review_task_id == good_preview.review_task_id
                    else task
                    for task in good_initialized.review_tasks
                )
            }
        )
    )

    service.reconcile_persisted_state()

    bad_current = service.get_project(bad_project.project_id)
    bad_current_preview = _task(bad_current, "preview_approval")
    assert bad_current_preview.attention is AttentionMode.MANUAL
    assert bad_current_preview.payload["status"] == "rendering"
    assert bad_current_preview.payload["claim_id"] == "foreign-claim"

    good_current = service.get_project(good_project.project_id)
    good_current_preview = _task(good_current, "preview_approval")
    assert good_current_preview.attention is AttentionMode.REVIEW
    assert good_current_preview.payload == {"status": "not_rendered"}
