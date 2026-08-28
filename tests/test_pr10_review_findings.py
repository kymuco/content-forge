from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from content_forge.api import create_app
from content_forge.application.review import (
    ReviewConflictError,
    ReviewService,
    ReviewValidationError,
)
from content_forge.core import (
    Asset,
    AssetRef,
    AttentionMode,
    EntityKind,
    MediaType,
    Project,
    ProjectState,
    ReviewStatus,
    ReviewTask,
)
from content_forge.orchestration import RenderJobIntegrityError
from content_forge.render.ffmpeg import FFmpegCapabilities
from content_forge.storage import LocalLibrary, StoredJob, transition_job_state
from content_forge.timeline import render_plan_digest


class PersistentFakeOrchestrator:
    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.plans: dict[str, tuple[object, str]] = {}
        self.artifacts: dict[str, object] = {}
        self.corrupt: set[str] = set()
        self.submit_count = 0
        self.on_run = None

    def submit(self, plan, *, purpose):
        self.submit_count += 1
        job = StoredJob(
            project_id=plan.project_id,
            job_type="render",
            state="queued",
            payload={
                "purpose": purpose,
                "profile_id": plan.output_profile.profile_id,
                "render_plan_digest": render_plan_digest(plan),
            },
        )
        self.library.database.create_job(job)
        self.plans[job.job_id] = (plan, purpose)
        return job

    def run_job(self, job_id, capabilities):
        job = self.library.database.get_job(job_id)
        assert job is not None
        if job.state == "queued":
            transition_job_state(
                self.library.database,
                job_id,
                expected_state="queued",
                state="running",
            )
        plan, purpose = self.plans[job_id]
        callback = self.on_run
        if callback is not None:
            self.on_run = None
            callback()
        artifact = SimpleNamespace(
            job_id=job_id,
            project_id=plan.project_id,
            purpose=purpose,
            profile_id=plan.output_profile.profile_id,
            render_plan_digest=render_plan_digest(plan),
            output_sha256=("a" if purpose == "preview" else "b") * 64,
            width=plan.output_profile.width,
            height=plan.output_profile.height,
            duration_seconds=plan.total_duration_seconds,
            output_storage_key=f"fake/{job_id}.mp4",
        )
        self.artifacts[job_id] = artifact
        transition_job_state(
            self.library.database,
            job_id,
            expected_state="running",
            state="succeeded",
        )
        return artifact

    def load_artifact(self, job_id, *, ffprobe_path="ffprobe", probe_timeout=20.0):
        if job_id in self.corrupt:
            raise RenderJobIntegrityError("synthetic corrupt artifact")
        return self.artifacts.get(job_id)


def _caps() -> FFmpegCapabilities:
    return FFmpegCapabilities(
        ffmpeg_path="/fake/ffmpeg",
        ffprobe_path="/fake/ffprobe",
        ffmpeg_version="fake",
        ffprobe_version="fake",
        encoders=("libx264",),
        filters=(),
    )


def _visual_project(library: LocalLibrary, *, count: int = 1) -> Project:
    refs = []
    for index in range(count):
        asset = library.database.put_asset(
            Asset(
                sha256=f"{index + 901:064x}",
                media_type=MediaType.IMAGE,
                mime_type="image/png",
                size_bytes=100 + index,
                width=1080,
                height=1920,
            )
        )
        refs.append(AssetRef(asset_id=asset.asset_id))
    return library.save_project(
        Project(
            content_kind="image_sequence" if count > 1 else "image",
            state=ProjectState.INBOX,
            source_refs=tuple(refs),
        )
    )


def _task(project: Project, task_type: str) -> ReviewTask:
    return next(task for task in project.review_tasks if task.task_type == task_type)


def _service(library: LocalLibrary, fake: PersistentFakeOrchestrator) -> ReviewService:
    return ReviewService(library, orchestrator=fake, capability_loader=_caps)


def _resolve_blockers(service: ReviewService, project: Project) -> Project:
    task = _task(project, "hook")
    project = service.resolve_task(project.project_id, task.review_task_id, "review hook")
    task = _task(project, "crop_confirmation")
    project = service.resolve_task(
        project.project_id,
        task.review_task_id,
        {"crops": {scene.scene_id: None for scene in project.scenes}},
    )
    order = next(
        (task for task in project.review_tasks if task.task_type == "source_order"),
        None,
    )
    if order is not None:
        project = service.resolve_task(
            project.project_id,
            order.review_task_id,
            [scene.scene_id for scene in sorted(project.scenes, key=lambda item: item.order)],
        )
    return project


def _ready_project(
    library: LocalLibrary,
    fake: PersistentFakeOrchestrator,
) -> tuple[ReviewService, Project, dict[str, object]]:
    service = _service(library, fake)
    project = service.bootstrap_project(_visual_project(library).project_id)
    project = _resolve_blockers(service, project)
    preview = service.render_preview(project.project_id)
    project = service.approve_preview(project.project_id, str(preview["job_id"]))
    assert project.state is ProjectState.READY
    return service, project, preview


def _persist_project(library: LocalLibrary, project: Project) -> Project:
    return library.save_project(project)


def test_repeated_bootstrap_preserves_ready_and_done_projects(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service, ready, _ = _ready_project(library, fake)

    same_ready = service.bootstrap_project(ready.project_id)
    assert same_ready.state is ProjectState.READY
    assert _task(same_ready, "preview_approval").status is ReviewStatus.RESOLVED

    final = service.render_final(ready.project_id)
    done = service.get_project(ready.project_id)
    assert done.state is ProjectState.DONE
    same_done = service.bootstrap_project(ready.project_id)
    assert same_done.state is ProjectState.DONE
    assert same_done.metadata["final_render_job_id"] == final["job_id"]


def test_phone_resolve_rejects_manual_and_unknown_review_tasks(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service = _service(library, fake)

    manual_project = library.save_project(Project(content_kind="note", state=ProjectState.INBOX))
    manual_project = service.bootstrap_project(manual_project.project_id)
    manual = _task(manual_project, "source_setup")
    assert manual.attention is AttentionMode.MANUAL
    with pytest.raises(ReviewValidationError):
        service.resolve_task(manual_project.project_id, manual.review_task_id, "unsafe close")

    project = service.bootstrap_project(_visual_project(library).project_id)
    future = ReviewTask(
        project_id=project.project_id,
        task_type="future_review_decision",
        attention=AttentionMode.REVIEW,
        blocking=True,
    )
    project = _persist_project(
        library,
        project.validated_copy(update={"review_tasks": project.review_tasks + (future,)}),
    )
    with pytest.raises(ReviewValidationError):
        service.resolve_task(project.project_id, future.review_task_id, {"arbitrary": True})


def test_metadata_edit_during_preview_keeps_candidate_stale(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service = _service(library, fake)
    project = service.bootstrap_project(_visual_project(library).project_id)
    project = _resolve_blockers(service, project)
    metadata = _task(project, "metadata")

    def edit_during_run() -> None:
        service.resolve_task(
            project.project_id,
            metadata.review_task_id,
            {"title": "concurrent edit", "description": None, "hashtags": []},
        )

    fake.on_run = edit_during_run
    with pytest.raises(ReviewConflictError):
        service.render_preview(project.project_id)

    current = service.get_project(project.project_id)
    assert _task(current, "metadata").status is ReviewStatus.RESOLVED
    assert _task(current, "preview_approval").payload["status"] == "not_rendered"


def test_concurrent_preview_requests_share_one_canonical_compute_claim(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service = _service(library, fake)
    project = service.bootstrap_project(_visual_project(library).project_id)
    project = _resolve_blockers(service, project)
    nested_conflicts = []

    def second_tab() -> None:
        with pytest.raises(ReviewConflictError):
            service.render_preview(project.project_id)
        nested_conflicts.append(True)

    fake.on_run = second_tab
    preview = service.render_preview(project.project_id)
    assert nested_conflicts == [True]
    assert fake.submit_count == 1
    assert _task(service.get_project(project.project_id), "preview_approval").payload["job_id"] == preview["job_id"]


def test_final_retry_after_lost_response_returns_same_authenticated_artifact(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service, project, _ = _ready_project(library, fake)

    first = service.render_final(project.project_id)
    submit_count = fake.submit_count
    second = service.render_final(project.project_id)

    assert second["job_id"] == first["job_id"]
    assert second["output_sha256"] == first["output_sha256"]
    assert fake.submit_count == submit_count
    summary = service.project_summary(service.get_project(project.project_id))
    assert summary["final"]["job_id"] == first["job_id"]


def test_restart_reconciliation_adopts_succeeded_final_and_finishes_qc(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service, project, _ = _ready_project(library, fake)
    final_plan = service._compile_plan(project, "shorts_final")
    final_digest = render_plan_digest(final_plan)
    job = fake.submit(final_plan, purpose="final")
    artifact = fake.run_job(job.job_id, _caps())

    rendering_metadata = dict(project.metadata)
    rendering_metadata["active_final_plan_digest"] = final_digest
    rendering = _persist_project(
        library,
        project.validated_copy(
            update={"state": ProjectState.RENDERING, "metadata": rendering_metadata}
        ),
    )
    recovered = _service(library, fake)
    recovered.reconcile_persisted_state()
    done = recovered.get_project(rendering.project_id)
    assert done.state is ProjectState.DONE
    assert done.metadata["final_render_job_id"] == artifact.job_id

    qc_metadata = dict(project.metadata)
    qc_metadata.update(
        {
            "final_render_job_id": artifact.job_id,
            "final_render_plan_digest": final_digest,
            "final_output_sha256": artifact.output_sha256,
        }
    )
    qc = _persist_project(
        library,
        project.validated_copy(update={"state": ProjectState.QC, "metadata": qc_metadata}),
    )
    recovered.reconcile_persisted_state()
    assert recovered.get_project(qc.project_id).state is ProjectState.DONE


def test_restart_reconciliation_releases_queued_and_retires_running_final_jobs(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service, project, _ = _ready_project(library, fake)
    final_plan = service._compile_plan(project, "shorts_final")
    final_digest = render_plan_digest(final_plan)

    queued = fake.submit(final_plan, purpose="final")
    metadata = dict(project.metadata)
    metadata["active_final_plan_digest"] = final_digest
    _persist_project(
        library,
        project.validated_copy(update={"state": ProjectState.RENDERING, "metadata": metadata}),
    )
    recovered = _service(library, fake)
    recovered.reconcile_persisted_state()
    assert recovered.get_project(project.project_id).state is ProjectState.READY
    assert library.database.get_job(queued.job_id).state == "queued"

    completed = recovered.render_final(project.project_id)
    assert completed["job_id"] == queued.job_id
    assert recovered.get_project(project.project_id).state is ProjectState.DONE

    service2, project2, _ = _ready_project(library, fake)
    final_plan2 = service2._compile_plan(project2, "shorts_final")
    digest2 = render_plan_digest(final_plan2)
    running = fake.submit(final_plan2, purpose="final")
    transition_job_state(
        library.database,
        running.job_id,
        expected_state="queued",
        state="running",
    )
    metadata2 = dict(project2.metadata)
    metadata2["active_final_plan_digest"] = digest2
    _persist_project(
        library,
        project2.validated_copy(update={"state": ProjectState.RENDERING, "metadata": metadata2}),
    )
    recovered.reconcile_persisted_state()
    assert recovered.get_project(project2.project_id).state is ProjectState.READY
    assert library.database.get_job(running.job_id).state == "failed"


def test_corrupt_reuse_candidate_does_not_wedge_fresh_preview(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service = _service(library, fake)
    project = service.bootstrap_project(_visual_project(library).project_id)
    project = _resolve_blockers(service, project)
    plan = service._compile_plan(project, "shorts_preview")

    corrupt_job = fake.submit(plan, purpose="preview")
    fake.run_job(corrupt_job.job_id, _caps())
    fake.corrupt.add(corrupt_job.job_id)

    preview = service.render_preview(project.project_id)
    assert preview["job_id"] != corrupt_job.job_id
    assert fake.submit_count == 2


def test_done_projects_do_not_leave_unactionable_optional_queue_cards(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = PersistentFakeOrchestrator(library)
    service, project, _ = _ready_project(library, fake)
    assert _task(project, "metadata").status is ReviewStatus.OPEN
    service.render_final(project.project_id)

    queue = service.list_queue()["items"]
    assert all(item["project_id"] != project.project_id for item in queue)


def test_api_startup_invokes_pr10_reconciliation(monkeypatch, tmp_path) -> None:
    calls = []
    original = ReviewService.reconcile_persisted_state

    def wrapped(self):
        calls.append(self.library.paths.root)
        return original(self)

    monkeypatch.setattr(ReviewService, "reconcile_persisted_state", wrapped)
    app = create_app(root=tmp_path)
    try:
        assert calls == [app.state.library.paths.root]
    finally:
        app.state.runtime_lease.close()


def test_pr10_worker_upgrade_versions_and_precaches_review_script(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v8" in worker.text
        assert 'appUrl("review.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
