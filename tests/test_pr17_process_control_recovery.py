from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import content_forge.batch.final as batch_final
import content_forge.orchestration._render_jobs_hardened as render_hardened
from content_forge.batch import BatchCoordinator, BatchRenderInput
from content_forge.core import AssetRef, MediaType, Project, Scene, TemplateRef, Variant
from content_forge.orchestration import RenderOrchestrator
from content_forge.profiles import shorts_preview_profile
from content_forge.render.ffmpeg import FFmpegCapabilities
from content_forge.storage import LocalLibrary, list_jobs, transition_job_state
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    compile_hook_overlay,
)
from content_forge.timeline import render_plan_digest
from content_forge.variants import localized_variant_snapshot


def _fixture(tmp_path: Path):
    library = LocalLibrary(tmp_path / "runtime")
    source = tmp_path / "source.ppm"
    source.write_text(
        "P3\n4 6\n255\n" + "\n".join(["20 80 200"] * 24) + "\n",
        encoding="ascii",
    )
    ingest = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
    )
    variant = Variant(language="en", hook="Process control remains recoverable")
    project = Project(
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
    )
    library.save_project(project)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id="shorts_preview",
        variant_id=variant.variant_id,
    )
    return library, project, plan, localized_variant_snapshot(variant)


def _capabilities() -> FFmpegCapabilities:
    return FFmpegCapabilities(
        ffmpeg_path="/definitely/missing/content-forge-ffmpeg",
        ffprobe_path="/definitely/missing/content-forge-ffprobe",
        ffmpeg_version="ffmpeg synthetic-test",
        ffprobe_version="ffprobe synthetic-test",
        encoders=("libx264",),
        filters=(
            "aformat",
            "amix",
            "anullsrc",
            "asetpts",
            "atrim",
            "color",
            "concat",
            "crop",
            "drawtext",
            "format",
            "fps",
            "overlay",
            "scale",
            "setpts",
            "trim",
            "volume",
        ),
    )


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_public_render_orchestrator_does_not_terminalize_process_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    library, _, plan, _ = _fixture(tmp_path)
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")

    def interrupt(*args, **kwargs):
        del args, kwargs
        raise interrupt_type()

    monkeypatch.setattr(render_hardened, "execute_ffmpeg", interrupt)

    with pytest.raises(interrupt_type):
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)

    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "running"
    assert "failure_manifest_digest" not in stored.payload
    assert "artifact_manifest_digest" not in stored.payload


def test_batch_keyboard_interrupt_leaves_parent_recoverable_and_retries_frozen_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, _, plan, localized = _fixture(tmp_path)
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

    def interrupt(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt()

    monkeypatch.setattr(render_hardened, "execute_ffmpeg", interrupt)

    with pytest.raises(KeyboardInterrupt):
        coordinator.run_batch(parent.job_id, _capabilities(), prefer_nvenc=False)

    stored_parent = library.database.get_job(parent.job_id)
    interrupted = library.database.get_job(item.initial_job_id)
    assert stored_parent is not None and stored_parent.state == "running"
    assert interrupted is not None and interrupted.state == "running"
    assert "batch_result_digest" not in stored_parent.payload
    assert "failure_manifest_digest" not in interrupted.payload

    retry, attempt_index = coordinator._current_attempt(  # noqa: SLF001
        parent.job_id,
        item,
        "restart-after-process-control",
    )

    assert attempt_index == 1
    assert retry.state == "queued"
    assert retry.payload["recovered_from_frozen_batch_plan"] is True
    assert render_plan_digest(coordinator.render.load_plan(retry.job_id)) == render_plan_digest(plan)
    old = library.database.get_job(item.initial_job_id)
    assert old is not None and old.state == "failed"
    failure = coordinator.render.load_failure(old.job_id)
    assert failure is not None and failure.code == "render_interrupted"


def test_process_control_during_qc_leaves_successful_child_and_running_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, _, plan, localized = _fixture(tmp_path)
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
        state="running",
    )
    transition_job_state(
        library.database,
        child.job_id,
        expected_state="running",
        state="succeeded",
    )

    monkeypatch.setattr(
        coordinator.render,
        "load_artifact",
        lambda *args, **kwargs: SimpleNamespace(output_storage_key="synthetic.mp4"),
    )

    def interrupt_qc(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt()

    monkeypatch.setattr(batch_final, "run_render_qc", interrupt_qc)

    with pytest.raises(KeyboardInterrupt):
        coordinator.run_batch(parent.job_id, _capabilities(), prefer_nvenc=False)

    stored_parent = library.database.get_job(parent.job_id)
    stored_child = library.database.get_job(child.job_id)
    assert stored_parent is not None and stored_parent.state == "running"
    assert stored_child is not None and stored_child.state == "succeeded"
    assert "batch_result_digest" not in stored_parent.payload


def test_failure_storm_uses_one_attempt_scan_including_outer_error_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, _, plan, localized = _fixture(tmp_path)
    coordinator = BatchCoordinator(library)
    parent = coordinator.prepare(
        [
            BatchRenderInput(
                plan=plan,
                purpose="preview",
                localized_variant=localized,
            )
            for _ in range(5)
        ]
    )
    manifest = coordinator.load_manifest(parent.job_id)
    for item in manifest.items:
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
            state="running",
        )
        transition_job_state(
            library.database,
            child.job_id,
            expected_state="running",
            state="succeeded",
        )

    original_list_jobs = batch_final.list_jobs
    scan_count = 0

    def counted_list_jobs(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        return original_list_jobs(*args, **kwargs)

    monkeypatch.setattr(batch_final, "list_jobs", counted_list_jobs)

    result = coordinator.run_batch(parent.job_id, _capabilities(), prefer_nvenc=False)

    assert result.status == "failed"
    assert len(result.items) == 5
    assert all(item.failure_code == "batch_item_failed" for item in result.items)
    assert scan_count == 1
    render_jobs = [
        job
        for job in list_jobs(library.database, job_type="render")
        if job.payload.get("batch_context", {}).get("batch_job_id") == parent.job_id
    ]
    assert len(render_jobs) == 5
