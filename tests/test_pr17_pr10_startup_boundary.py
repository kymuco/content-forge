from __future__ import annotations

from pathlib import Path

from content_forge.api import create_app
from content_forge.application import ReviewService
from content_forge.batch import BatchCoordinator, BatchRenderInput
from content_forge.core import Asset, AssetRef, MediaType, Project, ProjectState, ReviewStatus
from content_forge.storage import LocalLibrary, StoredJob, transition_job_state
from content_forge.timeline import render_plan_digest
from content_forge.variants import localized_variant_snapshot


def _task(project: Project, task_type: str):
    return next(task for task in project.review_tasks if task.task_type == task_type)


def _pr10_ready_for_preview(library: LocalLibrary) -> tuple[ReviewService, Project]:
    asset = library.database.put_asset(
        Asset(
            sha256="9" * 64,
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=100,
            width=1080,
            height=1920,
        )
    )
    project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )
    review = ReviewService(library)
    project = review.bootstrap_project(project.project_id)

    hook = _task(project, "hook")
    project = review.resolve_task(
        project.project_id,
        hook.review_task_id,
        "Batch startup recovery remains reproducible",
    )
    crop = _task(project, "crop_confirmation")
    project = review.resolve_task(
        project.project_id,
        crop.review_task_id,
        {"crops": {scene.scene_id: None for scene in project.scenes}},
    )
    order = next(
        (task for task in project.review_tasks if task.task_type == "source_order"),
        None,
    )
    if order is not None and order.status is ReviewStatus.OPEN:
        project = review.resolve_task(
            project.project_id,
            order.review_task_id,
            [scene.scene_id for scene in sorted(project.scenes, key=lambda value: value.order)],
        )
    return review, project


def test_create_app_does_not_retire_running_batch_preview_before_pr17_recovery(
    tmp_path: Path,
) -> None:
    library = LocalLibrary(tmp_path)
    review, project = _pr10_ready_for_preview(library)
    assert project.metadata["pr10_review_initialized"] is True

    plan = review._compile_plan(project, "shorts_preview")  # noqa: SLF001
    localized = None
    if plan.variant_id is not None:
        variant = next(
            value for value in project.variants if value.variant_id == plan.variant_id
        )
        localized = localized_variant_snapshot(variant)

    coordinator = BatchCoordinator(library)
    parent = coordinator.prepare(
        [
            BatchRenderInput(
                plan=plan,
                purpose="preview",
                localized_variant=localized,
            )
        ]
    )
    item = coordinator.load_manifest(parent.job_id).items[0]

    transition_job_state(
        library.database,
        parent.job_id,
        expected_state="queued",
        state="running",
    )
    child = transition_job_state(
        library.database,
        item.initial_job_id,
        expected_state="batch_held",
        state="queued",
        payload_additions={"batch_released": True},
    )
    child = transition_job_state(
        library.database,
        child.job_id,
        expected_state="queued",
        state="queued",
        payload_additions={"batch_run_instance_id": "process-before-restart"},
    )
    transition_job_state(
        library.database,
        child.job_id,
        expected_state="queued",
        state="running",
    )

    # This is the real startup order implicated by the review finding. PR10 may recover
    # its own claims, but it must not rewrite the state of a PR17-owned render attempt.
    app = create_app(root=tmp_path)
    try:
        startup_child = app.state.library.database.get_job(child.job_id)
        assert startup_child is not None
        assert startup_child.state == "running"
    finally:
        app.state.runtime_lease.close()

    recovered = BatchCoordinator(library)
    retry, attempt_index = recovered._current_attempt(  # noqa: SLF001
        parent.job_id,
        item,
        "process-after-restart",
    )

    assert attempt_index == 1
    assert retry.state == "queued"
    assert retry.payload["recovered_from_frozen_batch_plan"] is True
    assert render_plan_digest(recovered.render.load_plan(retry.job_id)) == item.render_plan_digest
    old = library.database.get_job(child.job_id)
    assert old is not None and old.state == "failed"
    failure = recovered.render.load_failure(old.job_id)
    assert failure is not None
    assert failure.code == "render_interrupted"


def test_pr10_startup_still_retires_its_own_non_batch_running_preview(
    tmp_path: Path,
) -> None:
    library = LocalLibrary(tmp_path)
    _, project = _pr10_ready_for_preview(library)
    ordinary = StoredJob(
        project_id=project.project_id,
        job_type="render",
        state="running",
        payload={"purpose": "preview"},
    )
    library.database.create_job(ordinary)

    app = create_app(root=tmp_path)
    try:
        persisted = app.state.library.database.get_job(ordinary.job_id)
        assert persisted is not None
        assert persisted.state == "failed"
    finally:
        app.state.runtime_lease.close()
