from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.application import _review_base as _base
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
    Variant,
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


def _repair_manual_project(
    library: LocalLibrary,
    service: ReviewService,
    seed: int,
) -> Project:
    project = library.save_project(Project(content_kind="image", state=ProjectState.INBOX))
    prepared = service.bootstrap_project(project.project_id)
    assert prepared.metadata.get("pr10_review_initialized") is True
    assert prepared.metadata.get("review_renderable") is False
    assert _task(prepared, "source_setup").status is ReviewStatus.OPEN

    asset = _image_asset(library, seed)
    repaired = prepared.validated_copy(
        update={
            "source_refs": (AssetRef(asset_id=asset.asset_id),),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    return library.save_project(repaired)


def test_uninitialized_reserved_authority_is_hidden_and_phone_ops_fail_closed(
    tmp_path,
) -> None:
    library = LocalLibrary(tmp_path)
    project = Project(content_kind="image", state=ProjectState.INBOX)
    variant = Variant()
    hook = ReviewTask(
        project_id=project.project_id,
        task_type="hook",
        attention=AttentionMode.REVIEW,
        priority=ReviewPriority.BLOCKING,
        blocking=True,
        payload={"variant_id": variant.variant_id, "current": None},
    )
    project = library.save_project(
        project.validated_copy(
            update={
                "variants": (variant,),
                "review_tasks": (hook,),
                "metadata": {"review_variant_id": variant.variant_id},
            }
        )
    )
    service = ReviewService(library)

    queue = service.list_queue()
    assert all(item["project_id"] != project.project_id for item in queue["items"])

    calls = (
        lambda: service.resolve_task(project.project_id, hook.review_task_id, "unsafe"),
        lambda: service.render_preview(project.project_id),
        lambda: service.approve_preview(project.project_id, "job_" + "1" * 32),
        lambda: service.reject_preview(project.project_id, "job_" + "1" * 32),
        lambda: service.render_final(project.project_id),
    )
    for call in calls:
        with pytest.raises(
            ReviewConflictError,
            match="project is not initialized for PR10 review",
        ):
            call()

    current = service.get_project(project.project_id)
    assert _task(current, "hook").status is ReviewStatus.OPEN
    assert current.variants[0].hook is None
    assert not current.metadata.get("pr10_review_initialized")


def test_manual_reentry_resumes_after_crash_before_base_bootstrap(
    tmp_path,
    monkeypatch,
) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)
    repaired = _repair_manual_project(library, service, 1601)

    original_bootstrap = _base.ReviewService.bootstrap_project

    def crash_before_base(self, project_id: str):
        raise RuntimeError("simulated crash before base bootstrap")

    monkeypatch.setattr(_base.ReviewService, "bootstrap_project", crash_before_base)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.bootstrap_project(repaired.project_id)

    checkpoint = service.get_project(repaired.project_id)
    assert checkpoint.metadata.get("pr10_manual_reentry_pending") is True
    assert not checkpoint.metadata.get("pr10_review_initialized")
    assert _task(checkpoint, "source_setup").status is ReviewStatus.OPEN

    monkeypatch.setattr(_base.ReviewService, "bootstrap_project", original_bootstrap)
    resumed = service.bootstrap_project(repaired.project_id)

    assert resumed.metadata.get("pr10_review_initialized") is True
    assert resumed.metadata.get("review_renderable") is True
    assert "pr10_manual_reentry_pending" not in resumed.metadata
    source_setup = _task(resumed, "source_setup")
    assert source_setup.status is ReviewStatus.RESOLVED
    assert source_setup.accepted_value == "manual_setup_completed"
    assert _task(resumed, "preview_approval").status is ReviewStatus.OPEN


def test_manual_reentry_resumes_after_crash_after_base_bootstrap(
    tmp_path,
    monkeypatch,
) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)
    repaired = _repair_manual_project(library, service, 1602)

    original_finalize = service._finalize_bootstrap_payloads

    def crash_after_base(project_id: str, *, manual_reentry: bool):
        assert manual_reentry is True
        raise RuntimeError("simulated crash after base bootstrap")

    monkeypatch.setattr(service, "_finalize_bootstrap_payloads", crash_after_base)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.bootstrap_project(repaired.project_id)

    checkpoint = service.get_project(repaired.project_id)
    assert checkpoint.metadata.get("pr10_manual_reentry_pending") is True
    assert checkpoint.metadata.get("pr10_review_initialized") is True
    assert checkpoint.metadata.get("review_renderable") is True
    assert _task(checkpoint, "source_setup").status is ReviewStatus.OPEN
    assert _task(checkpoint, "preview_approval").status is ReviewStatus.OPEN

    monkeypatch.setattr(service, "_finalize_bootstrap_payloads", original_finalize)
    resumed = service.bootstrap_project(repaired.project_id)

    assert resumed.metadata.get("pr10_review_initialized") is True
    assert resumed.metadata.get("review_renderable") is True
    assert "pr10_manual_reentry_pending" not in resumed.metadata
    source_setup = _task(resumed, "source_setup")
    assert source_setup.status is ReviewStatus.RESOLVED
    assert source_setup.accepted_value == "manual_setup_completed"


def test_ready_projects_list_requires_pr10_approved_preview_receipt(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)

    legacy = library.save_project(
        Project(content_kind="image", state=ProjectState.READY)
    )

    asset = _image_asset(library, 1603)
    project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )
    prepared = service.bootstrap_project(project.project_id)
    hook = _task(prepared, "hook")
    prepared = service.resolve_task(prepared.project_id, hook.review_task_id, "ready hook")
    crop = _task(prepared, "crop_confirmation")
    scene_id = prepared.scenes[0].scene_id
    prepared = service.resolve_task(
        prepared.project_id,
        crop.review_task_id,
        {"crops": {scene_id: None}},
    )

    preview = _task(prepared, "preview_approval")
    now = datetime.now(timezone.utc)
    job_id = "job_" + "2" * 32
    plan_digest = "a" * 64
    resolved_preview = preview.validated_copy(
        update={
            "status": ReviewStatus.RESOLVED,
            "accepted_value": job_id,
            "resolved_at": now,
            "payload": {
                "status": "ready",
                "job_id": job_id,
                "render_plan_digest": plan_digest,
                "project_revision_digest": "b" * 64,
            },
        }
    )
    tasks = tuple(
        resolved_preview if task.review_task_id == preview.review_task_id else task
        for task in prepared.review_tasks
    )
    metadata = dict(prepared.metadata)
    metadata["approved_preview_job_id"] = job_id
    metadata["approved_preview_plan_digest"] = plan_digest
    ready = prepared.validated_copy(
        update={
            "state": ProjectState.READY,
            "review_tasks": tasks,
            "metadata": metadata,
            "updated_at": now,
        }
    )
    metadata = dict(ready.metadata)
    metadata["approved_preview_revision_digest"] = _base._preview_revision_digest(ready)
    ready = library.save_project(ready.validated_copy(update={"metadata": metadata}))

    queue = service.list_queue()
    ready_ids = [item["project_id"] for item in queue["ready_projects"]]
    assert ready.project_id in ready_ids
    assert legacy.project_id not in ready_ids
