from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from content_forge.batch import (
    BatchCoordinator,
    BatchPreparationError,
    BatchRenderInput,
)
from content_forge.core import (
    AssetRef,
    MediaType,
    Project,
    ReviewStatus,
    ReviewSuggestion,
    ReviewTask,
    Scene,
    TemplateRef,
    Variant,
)
from content_forge.profiles import shorts_preview_profile
from content_forge.storage import LocalLibrary, list_jobs, transition_job_state
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    compile_hook_overlay,
)
from content_forge.timeline import render_plan_digest
from content_forge.variants import localized_variant_snapshot


def _project(library: LocalLibrary, tmp_path: Path) -> tuple[Project, object]:
    source = tmp_path / "source.ppm"
    source.write_text(
        "P3\n4 4\n255\n" + "\n".join(["220 30 60", "30 100 220"] * 8) + "\n",
        encoding="ascii",
    )
    ingest = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
    )
    variant = Variant(language="en", hook="Persistent batch render works")
    suggestion = ReviewSuggestion(
        label="Persistent batch render works",
        value="Persistent batch render works",
        provider="chatgpt-web-adapter",
        metadata={
            "task": "hook",
            "request_sha256": "1" * 64,
            "response_sha256": "2" * 64,
            "model_profile": "fast",
        },
    )
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    review = ReviewTask(
        project_id="cf_project_" + "8" * 32,
        task_type="hook",
        status=ReviewStatus.RESOLVED,
        suggestions=(suggestion,),
        accepted_value="Persistent batch render works",
        created_at=stamp,
        resolved_at=stamp,
    )
    project = Project(
        project_id=review.project_id,
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        variants=(variant,),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.35,
                media=AssetRef(asset_id=ingest.asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
        review_tasks=(review,),
        created_at=stamp,
        updated_at=stamp,
    )
    library.save_project(project)
    return project, ingest.asset


def _prepared(tmp_path: Path):
    library = LocalLibrary(tmp_path / "runtime")
    project, _ = _project(library, tmp_path)
    plan = compile_hook_overlay(project, library.database)
    localized = localized_variant_snapshot(project.variants[0])
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
    return library, project, plan, coordinator, parent


def test_prepare_freezes_accepted_provider_state_and_links_render_attempt(tmp_path: Path) -> None:
    library, project, plan, coordinator, parent = _prepared(tmp_path)

    assert parent.state == "queued"
    manifest = coordinator.load_manifest(parent.job_id)
    assert len(manifest.items) == 1
    item = manifest.items[0]
    assert item.project_id == project.project_id
    assert item.render_plan_digest == render_plan_digest(plan)
    assert item.localized_variant is not None
    assert item.localized_variant.hook == "Persistent batch render works"
    assert item.accepted_state.rendered_text == {
        overlay.overlay_id: overlay.text
        for overlay in plan.overlays
        if overlay.text is not None
    }
    assert item.accepted_state.provider_parameters[0].provider == "chatgpt-web-adapter"
    assert item.accepted_state.provider_parameters[0].metadata["request_sha256"] == "1" * 64

    child = library.database.get_job(item.initial_job_id)
    assert child is not None
    assert child.state == "queued"
    assert child.payload["batch_context"] == {
        "batch_job_id": parent.job_id,
        "item_key": "item_0000",
        "attempt_index": 0,
    }
    assert [job.job_id for job in list_jobs(library.database, job_type="batch")] == [
        parent.job_id
    ]


def test_prepare_rejects_localized_snapshot_after_project_metadata_changes(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project, _ = _project(library, tmp_path)
    plan = compile_hook_overlay(project, library.database)
    frozen = localized_variant_snapshot(project.variants[0])
    edited = project.variants[0].validated_copy(update={"title": "edited after compile"})
    library.save_project(project.validated_copy(update={"variants": (edited,)}))

    with pytest.raises(BatchPreparationError, match="localized metadata changed"):
        BatchCoordinator(library).prepare(
            [BatchRenderInput(plan=plan, purpose="preview", localized_variant=frozen)]
        )

    batches = list_jobs(library.database, job_type="batch")
    assert batches[-1].state == "failed"


def test_recovery_terminalizes_old_running_attempt_and_retries_frozen_plan_after_project_edit(
    tmp_path: Path,
) -> None:
    library, project, plan, coordinator, parent = _prepared(tmp_path)
    manifest = coordinator.load_manifest(parent.job_id)
    item = manifest.items[0]
    child = library.database.get_job(item.initial_job_id)
    assert child is not None

    child = transition_job_state(
        library.database,
        child.job_id,
        expected_state="queued",
        state="queued",
        payload_additions={"batch_run_instance_id": "old-process-run"},
    )
    transition_job_state(
        library.database,
        child.job_id,
        expected_state="queued",
        state="running",
    )

    # Recovery is tied to the batch's persisted plan, not mutable project metadata.
    edited_variant = project.variants[0].validated_copy(
        update={"hook": "Project changed after batch preparation"}
    )
    library.save_project(project.validated_copy(update={"variants": (edited_variant,)}))

    retry, attempt_index = coordinator._current_attempt(  # noqa: SLF001 - contract regression
        parent.job_id,
        item,
        "new-process-run",
    )

    assert attempt_index == 1
    assert retry.state == "queued"
    assert retry.job_id != child.job_id
    assert retry.payload["recovered_from_frozen_batch_plan"] is True
    old = library.database.get_job(child.job_id)
    assert old is not None and old.state == "failed"
    failure = coordinator.render.load_failure(old.job_id)
    assert failure is not None
    assert failure.code == "render_interrupted"
    assert render_plan_digest(coordinator.render.load_plan(retry.job_id)) == render_plan_digest(plan)

    attempts = [
        job
        for job in list_jobs(library.database, job_type="render")
        if getattr(job.payload.get("batch_context"), "get", lambda *_: None)(
            "batch_job_id"
        )
        == parent.job_id
    ]
    assert len(attempts) == 2


def test_recovery_replaces_stale_queued_claim_before_render_started(tmp_path: Path) -> None:
    library, _, plan, coordinator, parent = _prepared(tmp_path)
    item = coordinator.load_manifest(parent.job_id).items[0]
    child = library.database.get_job(item.initial_job_id)
    assert child is not None

    transition_job_state(
        library.database,
        child.job_id,
        expected_state="queued",
        state="queued",
        payload_additions={"batch_run_instance_id": "old-process-run"},
    )

    retry, attempt_index = coordinator._current_attempt(  # noqa: SLF001 - contract regression
        parent.job_id,
        item,
        "new-process-run",
    )

    assert attempt_index == 1
    assert retry.state == "queued"
    assert retry.job_id != child.job_id
    assert retry.payload["recovered_from_frozen_batch_plan"] is True
    stale = library.database.get_job(child.job_id)
    assert stale is not None and stale.state == "failed"
    failure = coordinator.render.load_failure(stale.job_id)
    assert failure is not None
    assert failure.code == "batch_claim_interrupted"
    assert render_plan_digest(coordinator.render.load_plan(retry.job_id)) == render_plan_digest(plan)
