from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from content_forge.api import create_app
from content_forge.application.review import ReviewConflictError, ReviewService
from content_forge.core import (
    Asset,
    AssetRef,
    AttentionMode,
    EntityKind,
    MediaType,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
    new_entity_id,
)
from content_forge.render.ffmpeg import FFmpegCapabilities
from content_forge.storage import LocalLibrary
from content_forge.timeline import render_plan_digest

LOOPBACK_HEADERS = {"Host": "localhost"}


class FakeOrchestrator:
    def __init__(self) -> None:
        self.plans = {}
        self.artifacts = {}

    def submit(self, plan, *, purpose):
        job_id = new_entity_id(EntityKind.JOB)
        self.plans[job_id] = (plan, purpose)
        return SimpleNamespace(job_id=job_id)

    def run_job(self, job_id, capabilities):
        plan, purpose = self.plans[job_id]
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
        return artifact

    def load_artifact(self, job_id, *, ffprobe_path="ffprobe", probe_timeout=20.0):
        return self.artifacts.get(job_id)


def _capabilities() -> FFmpegCapabilities:
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
                sha256=f"{index + 1:064x}",
                media_type=MediaType.IMAGE,
                mime_type="image/png",
                size_bytes=100 + index,
                width=1080,
                height=1920,
            )
        )
        refs.append(AssetRef(asset_id=asset.asset_id))
    project = Project(
        content_kind="image_sequence" if count > 1 else "image",
        state=ProjectState.INBOX,
        source_refs=tuple(refs),
    )
    return library.save_project(project)


def _task(project: Project, task_type: str):
    return next(task for task in project.review_tasks if task.task_type == task_type)


def _resolve_render_blockers(service: ReviewService, project: Project) -> Project:
    hook = _task(project, "hook")
    project = service.resolve_task(
        project.project_id,
        hook.review_task_id,
        "A bounded review hook",
    )
    crop = _task(project, "crop_confirmation")
    project = service.resolve_task(
        project.project_id,
        crop.review_task_id,
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


def _paired_headers(client: TestClient) -> dict[str, str]:
    challenge = client.post("/api/v1/pairing/challenges", headers=LOOPBACK_HEADERS)
    assert challenge.status_code == 201
    payload = challenge.json()
    exchanged = client.post(
        "/api/v1/pairing/exchange",
        headers=LOOPBACK_HEADERS,
        json={
            "challenge_id": payload["challenge_id"],
            "code": payload["code"],
            "label": "pr10-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def test_bootstrap_operationalizes_existing_review_contracts(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = _visual_project(library, count=2)
    service = ReviewService(library)

    bootstrapped = service.bootstrap_project(project.project_id)

    assert bootstrapped.state is ProjectState.NEEDS_REVIEW
    assert bootstrapped.template is not None
    assert bootstrapped.template.template_id == "hook_overlay"
    assert {profile.profile_id for profile in bootstrapped.output_profiles} == {
        "shorts_preview",
        "shorts_final",
    }
    assert [(profile.width, profile.height) for profile in bootstrapped.output_profiles] == [
        (540, 960),
        (1080, 1920),
    ]
    assert len(bootstrapped.variants) == 1
    assert len(bootstrapped.scenes) == 2
    assert [scene.order for scene in bootstrapped.scenes] == [0, 1]

    auto = _task(bootstrapped, "timeline_bootstrap")
    assert auto.attention is AttentionMode.AUTO
    assert auto.status is ReviewStatus.RESOLVED
    assert auto.blocking is False

    assert _task(bootstrapped, "hook").priority is ReviewPriority.BLOCKING
    assert _task(bootstrapped, "crop_confirmation").blocking is True
    assert _task(bootstrapped, "source_order").blocking is True
    assert _task(bootstrapped, "metadata").blocking is False
    assert _task(bootstrapped, "preview_approval").blocking is True

    again = service.bootstrap_project(project.project_id)
    assert [task.review_task_id for task in again.review_tasks] == [
        task.review_task_id for task in bootstrapped.review_tasks
    ]


def test_review_queue_prioritizes_blocking_human_attention_and_hides_auto(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = _visual_project(library)
    service = ReviewService(library)
    service.bootstrap_project(project.project_id)

    payload = service.list_queue()
    items = payload["items"]
    assert items
    assert all(item["task"]["attention"] != "auto" for item in items)
    first_nonblocking = next(
        (index for index, item in enumerate(items) if not item["task"]["blocking"]),
        len(items),
    )
    assert all(item["task"]["blocking"] for item in items[:first_nonblocking])


def test_review_decisions_compile_same_project_into_540x960_proxy(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = _visual_project(library, count=2)
    service = ReviewService(library)
    project = service.bootstrap_project(project.project_id)
    project = _resolve_render_blockers(service, project)

    plan = service._compile_plan(project, "shorts_preview")

    assert plan.output_profile.width == 540
    assert plan.output_profile.height == 960
    assert plan.output_profile.properties["purpose"] == "preview"
    assert plan.template_id == "hook_overlay"
    assert len(plan.scenes) == 2


def test_preview_approval_is_digest_bound_and_final_reaches_done(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = _visual_project(library)
    fake = FakeOrchestrator()
    service = ReviewService(
        library,
        orchestrator=fake,
        capability_loader=_capabilities,
    )
    project = service.bootstrap_project(project.project_id)
    project = _resolve_render_blockers(service, project)

    preview = service.render_preview(project.project_id)
    assert preview["width"] == 540
    assert preview["height"] == 960

    project = service.approve_preview(project.project_id, preview["job_id"])
    assert project.state is ProjectState.READY
    assert project.metadata["approved_preview_job_id"] == preview["job_id"]

    final = service.render_final(project.project_id)
    assert final["width"] == 1080
    assert final["height"] == 1920
    assert service.get_project(project.project_id).state is ProjectState.DONE


def test_edit_after_preview_invalidates_candidate_and_blocks_old_approval(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = _visual_project(library)
    fake = FakeOrchestrator()
    service = ReviewService(
        library,
        orchestrator=fake,
        capability_loader=_capabilities,
    )
    project = service.bootstrap_project(project.project_id)
    project = _resolve_render_blockers(service, project)
    preview = service.render_preview(project.project_id)

    metadata = _task(service.get_project(project.project_id), "metadata")
    service.resolve_task(
        project.project_id,
        metadata.review_task_id,
        {"title": "changed after preview", "description": None, "hashtags": []},
    )

    current = service.get_project(project.project_id)
    assert _task(current, "preview_approval").payload["status"] == "not_rendered"
    with pytest.raises(ReviewConflictError):
        service.approve_preview(project.project_id, preview["job_id"])


def test_reject_preview_reopens_editable_decisions(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = _visual_project(library)
    fake = FakeOrchestrator()
    service = ReviewService(
        library,
        orchestrator=fake,
        capability_loader=_capabilities,
    )
    project = service.bootstrap_project(project.project_id)
    project = _resolve_render_blockers(service, project)
    preview = service.render_preview(project.project_id)

    rejected = service.reject_preview(project.project_id, preview["job_id"])
    assert rejected.state is ProjectState.NEEDS_REVIEW
    assert _task(rejected, "hook").status is ReviewStatus.OPEN
    assert _task(rejected, "crop_confirmation").status is ReviewStatus.OPEN
    assert _task(rejected, "preview_approval").payload["status"] == "rejected"


def test_review_http_auth_precedes_json_parsing_and_body_is_bounded(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        project = _visual_project(app.state.library)
        headers = _paired_headers(client)
        bootstrapped = client.post(
            f"/api/v1/projects/{project.project_id}/review/bootstrap",
            headers=headers,
        )
        assert bootstrapped.status_code == 200
        task_id = next(
            item["task"]["review_task_id"]
            for item in client.get("/api/v1/review-queue", headers=headers).json()["items"]
            if item["task"]["task_type"] == "hook"
        )

        unauthenticated = client.post(
            f"/api/v1/projects/{project.project_id}/review/{task_id}/resolve",
            content=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert unauthenticated.status_code == 401

        request = client.build_request(
            "POST",
            f"/api/v1/projects/{project.project_id}/review/{task_id}/resolve",
            content=b'{"value":"ok"}',
            headers={
                **headers,
                "Content-Type": "application/json",
                "Content-Length": str(128 * 1024 + 1),
            },
        )
        request.headers["content-length"] = str(128 * 1024 + 1)
        oversized = client.send(request)
        assert oversized.status_code == 413
    finally:
        app.state.runtime_lease.close()


def test_review_routes_remain_protected_when_app_is_mounted(tmp_path) -> None:
    child = create_app(root=tmp_path)
    parent = FastAPI()
    parent.mount("/content-forge", child)
    client = TestClient(parent)
    try:
        project = _visual_project(child.state.library)
        blocked = client.post(
            f"/content-forge/api/v1/projects/{project.project_id}/review/fake/resolve",
            content=b"{broken",
            headers={
                "Host": "localhost",
                "Content-Type": "application/json",
            },
        )
        assert blocked.status_code == 401
    finally:
        child.state.runtime_lease.close()


def test_pwa_exposes_review_surface_without_untrusted_html_insertion(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/")
        script = client.get("/app/review.js")
        assert shell.status_code == 200
        assert script.status_code == 200
        assert 'id="review-panel"' in shell.text
        assert 'src="review.js"' in shell.text
        assert "innerHTML" not in script.text
        assert "render-jobs/" in script.text
        assert "Prepare Inbox projects" in shell.text
    finally:
        app.state.runtime_lease.close()
