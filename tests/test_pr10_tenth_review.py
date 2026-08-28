from __future__ import annotations

from types import SimpleNamespace

import pytest

from content_forge.application.review import ReviewConflictError, ReviewService
from content_forge.core import Asset, AssetRef, MediaType, Project, ProjectState, ReviewStatus
from content_forge.storage import LocalLibrary
from content_forge.timeline import render_plan_digest


class MissingArtifactLoader:
    def __init__(self) -> None:
        self.load_count = 0

    def load_artifact(self, job_id, *, ffprobe_path="ffprobe", probe_timeout=20.0):
        self.load_count += 1
        return None


def _visual_project(library: LocalLibrary) -> Project:
    asset = library.database.put_asset(
        Asset(
            sha256="a" * 64,
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=101,
            width=1080,
            height=1920,
        )
    )
    return library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )


def _task(project: Project, task_type: str):
    return next(task for task in project.review_tasks if task.task_type == task_type)


def _resolve_final_inputs(service: ReviewService, project: Project) -> Project:
    hook = _task(project, "hook")
    project = service.resolve_task(
        project.project_id,
        hook.review_task_id,
        "tenth-review hook",
    )
    crop = _task(project, "crop_confirmation")
    return service.resolve_task(
        project.project_id,
        crop.review_task_id,
        {"crops": {scene.scene_id: None for scene in project.scenes}},
    )


def test_incomplete_qc_receipt_with_stale_digest_reopens_review(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    bootstrap = ReviewService(library)
    project = bootstrap.bootstrap_project(_visual_project(library).project_id)
    project = _resolve_final_inputs(bootstrap, project)
    old_digest = render_plan_digest(bootstrap._compile_plan(project, "shorts_final"))

    variant = project.variants[0].validated_copy(
        update={"hook": "canonical semantics changed after retained digest"}
    )
    metadata = dict(project.metadata)
    metadata["final_render_plan_digest"] = old_digest
    metadata.pop("final_render_job_id", None)
    metadata.pop("final_output_sha256", None)
    library.save_project(
        project.validated_copy(
            update={
                "state": ProjectState.QC,
                "variants": (variant, *project.variants[1:]),
                "metadata": metadata,
            }
        )
    )

    loader = MissingArtifactLoader()
    service = ReviewService(library, orchestrator=loader)
    service.reconcile_persisted_state()

    recovered = service.get_project(project.project_id)
    assert recovered.state is ProjectState.NEEDS_REVIEW
    assert "final_render_plan_digest" not in recovered.metadata
    assert recovered.metadata["last_final_render_error"] == (
        "stale final QC receipt returned project to review"
    )
    assert _task(recovered, "hook").status is ReviewStatus.OPEN
    assert _task(recovered, "crop_confirmation").status is ReviewStatus.OPEN
    assert _task(recovered, "preview_approval").payload == {"status": "not_rendered"}
    assert loader.load_count == 0


def test_post_render_qc_failure_recovers_ready_in_same_call(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    bootstrap = ReviewService(library)
    project = bootstrap.bootstrap_project(_visual_project(library).project_id)
    project = _resolve_final_inputs(bootstrap, project)
    final_digest = render_plan_digest(bootstrap._compile_plan(project, "shorts_final"))

    metadata = dict(project.metadata)
    metadata["active_final_plan_digest"] = final_digest
    library.save_project(
        project.validated_copy(
            update={"state": ProjectState.RENDERING, "metadata": metadata}
        )
    )

    artifact = SimpleNamespace(
        job_id="job_01tenthpassmissingartifact000000000000",
        project_id=project.project_id,
        purpose="final",
        render_plan_digest=final_digest,
        output_sha256="b" * 64,
    )
    loader = MissingArtifactLoader()
    service = ReviewService(library, orchestrator=loader)

    with pytest.raises(ReviewConflictError, match="failed QC integrity validation"):
        service._record_final_success(project.project_id, artifact, final_digest)

    recovered = service.get_project(project.project_id)
    assert recovered.state is ProjectState.READY
    assert "active_final_plan_digest" not in recovered.metadata
    assert "final_render_job_id" not in recovered.metadata
    assert "final_render_plan_digest" not in recovered.metadata
    assert "final_output_sha256" not in recovered.metadata
    assert recovered.metadata["last_final_render_error"] == (
        "final artifact failed QC integrity validation"
    )
    assert loader.load_count == 1
