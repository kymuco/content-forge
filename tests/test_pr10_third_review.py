from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
from content_forge.render.ffmpeg import FFmpegCapabilities
from content_forge.storage import LocalLibrary
from content_forge.timeline import render_plan_digest


class FakeOrchestrator:
    def __init__(self) -> None:
        self.plans = {}
        self.artifacts = {}

    def submit(self, plan, *, purpose):
        job_id = f"job_{len(self.plans) + 1:032x}"
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
            output_sha256=("c" if purpose == "preview" else "d") * 64,
            width=plan.output_profile.width,
            height=plan.output_profile.height,
            duration_seconds=plan.total_duration_seconds,
            output_storage_key=f"fake/{job_id}.mp4",
        )
        self.artifacts[job_id] = artifact
        return artifact

    def load_artifact(self, job_id, *, ffprobe_path="ffprobe", probe_timeout=20.0):
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


def _service(library: LocalLibrary) -> ReviewService:
    return ReviewService(
        library,
        orchestrator=FakeOrchestrator(),
        capability_loader=_caps,
    )


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


def test_uninitialized_ready_project_cannot_be_rewound_by_bootstrap(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = library.save_project(
        Project(content_kind="image", state=ProjectState.READY)
    )
    service = _service(library)

    with pytest.raises(ReviewConflictError, match="cannot be bootstrapped from state ready"):
        service.bootstrap_project(project.project_id)

    current = service.get_project(project.project_id)
    assert current.state is ProjectState.READY
    assert not current.metadata.get("pr10_review_initialized")
    assert current.review_tasks == ()


def test_reserved_preview_task_authority_collision_fails_closed(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    project = Project(content_kind="image", state=ProjectState.INBOX)
    collision = ReviewTask(
        project_id=project.project_id,
        task_type="preview_approval",
        attention=AttentionMode.MANUAL,
        priority=ReviewPriority.BLOCKING,
        blocking=True,
        payload={"reason": "foreign manual authority"},
    )
    project = library.save_project(
        project.validated_copy(update={"review_tasks": (collision,)})
    )
    service = _service(library)

    # Uninitialized projects own none of PR10's reserved task namespace. The newer
    # lifecycle fence may therefore reject this before the narrower authority-shape check.
    with pytest.raises(ReviewConflictError, match="reserved review task"):
        service.bootstrap_project(project.project_id)

    current = service.get_project(project.project_id)
    assert current.state is ProjectState.INBOX
    assert current.review_tasks == (collision,)
    assert not current.metadata.get("pr10_review_initialized")


def test_reject_preserves_non_null_canonical_crop_through_phone_resave(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    asset = _image_asset(library, 1401)
    project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )
    service = _service(library)
    project = service.bootstrap_project(project.project_id)

    hook = _task(project, "hook")
    project = service.resolve_task(project.project_id, hook.review_task_id, "crop-safe hook")
    crop_task = _task(project, "crop_confirmation")
    scene_id = project.scenes[0].scene_id
    crop = {"x": 0.1, "y": 0.2, "width": 0.7, "height": 0.6}
    project = service.resolve_task(
        project.project_id,
        crop_task.review_task_id,
        {"crops": {scene_id: crop}},
    )
    assert project.scenes[0].crop is not None
    assert project.scenes[0].crop.model_dump(mode="json") == crop

    preview = service.render_preview(project.project_id)
    reopened = service.reject_preview(project.project_id, str(preview["job_id"]))
    reopened_crop = _task(reopened, "crop_confirmation")

    assert reopened_crop.status is ReviewStatus.OPEN
    assert list(reopened_crop.payload["scene_ids"]) == [scene_id]
    stored_crop = dict(reopened_crop.payload["crops"])[scene_id]
    assert stored_crop == crop

    # JSON serialization in the PWA thaws the canonical frozen mapping into an ordinary
    # JSON object. Submit that exact wire shape back through the phone resolve boundary.
    wire_crop = dict(stored_crop)
    saved = service.resolve_task(
        reopened.project_id,
        reopened_crop.review_task_id,
        {"crops": {scene_id: wire_crop}},
    )
    assert saved.scenes[0].crop is not None
    assert saved.scenes[0].crop.model_dump(mode="json") == crop

    review_js = Path("src/content_forge/web/static/review.js").read_text(encoding="utf-8")
    assert "item.task.payload.crops" in review_js
    assert 'button("Confirm current crop"' in review_js
    assert 'button("Confirm full frame"' not in review_js
