from __future__ import annotations

from types import SimpleNamespace

from content_forge.application.review import ReviewService
from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    Project,
    ProjectState,
    ReviewStatus,
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


def _caps() -> FFmpegCapabilities:
    return FFmpegCapabilities(
        ffmpeg_path="/fake/ffmpeg",
        ffprobe_path="/fake/ffprobe",
        ffmpeg_version="fake",
        ffprobe_version="fake",
        encoders=("libx264",),
        filters=(),
    )


def _task(project: Project, task_type: str):
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


def _resolve_blockers(service: ReviewService, project: Project) -> Project:
    hook = _task(project, "hook")
    project = service.resolve_task(project.project_id, hook.review_task_id, "edited hook")
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
        current_ids = [scene.scene_id for scene in sorted(project.scenes, key=lambda item: item.order)]
        project = service.resolve_task(
            project.project_id,
            order.review_task_id,
            list(reversed(current_ids)),
        )
    return project


def test_reject_rehydrates_reopened_payloads_from_canonical_edits(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    first = _image_asset(library, 1201)
    second = _image_asset(library, 1202)
    project = library.save_project(
        Project(
            content_kind="image_sequence",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=first.asset_id), AssetRef(asset_id=second.asset_id)),
        )
    )
    fake = FakeOrchestrator()
    service = ReviewService(library, orchestrator=fake, capability_loader=_caps)
    project = service.bootstrap_project(project.project_id)
    project = _resolve_blockers(service, project)

    metadata = _task(project, "metadata")
    project = service.resolve_task(
        project.project_id,
        metadata.review_task_id,
        {
            "title": "edited title",
            "description": "edited description",
            "hashtags": ["edited", "canonical"],
        },
    )
    canonical_order = [
        scene.scene_id for scene in sorted(project.scenes, key=lambda item: item.order)
    ]

    preview = service.render_preview(project.project_id)
    reopened = service.reject_preview(project.project_id, str(preview["job_id"]))

    hook = _task(reopened, "hook")
    order = _task(reopened, "source_order")
    metadata = _task(reopened, "metadata")
    assert hook.status is ReviewStatus.OPEN
    assert hook.payload["current"] == "edited hook"
    assert order.status is ReviewStatus.OPEN
    assert list(order.payload["scene_ids"]) == canonical_order
    assert metadata.status is ReviewStatus.OPEN
    assert metadata.payload["title"] == "edited title"
    assert metadata.payload["description"] == "edited description"
    assert list(metadata.payload["hashtags"]) == ["edited", "canonical"]

    # Saving the reopened values supplied by the phone must preserve, not roll back,
    # the canonical edits that produced the rejected preview.
    after_order = service.resolve_task(
        reopened.project_id,
        order.review_task_id,
        list(order.payload["scene_ids"]),
    )
    assert [
        scene.scene_id for scene in sorted(after_order.scenes, key=lambda item: item.order)
    ] == canonical_order


def test_manual_setup_rebootstrap_promotes_repaired_project_and_keeps_ready_stable(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    fake = FakeOrchestrator()
    service = ReviewService(library, orchestrator=fake, capability_loader=_caps)
    project = library.save_project(Project(content_kind="image", state=ProjectState.INBOX))

    manual = service.bootstrap_project(project.project_id)
    assert manual.state is ProjectState.NEEDS_REVIEW
    assert manual.metadata["review_renderable"] is False
    source_setup = _task(manual, "source_setup")
    assert source_setup.status is ReviewStatus.OPEN

    # Simulate the desktop/manual step requested by source_setup: authoritative project
    # source membership is repaired outside the bounded phone resolve surface.
    asset = _image_asset(library, 1301)
    repaired = library.save_project(
        manual.validated_copy(
            update={"source_refs": (AssetRef(asset_id=asset.asset_id),)}
        )
    )

    promoted = service.bootstrap_project(repaired.project_id)
    assert promoted.state is ProjectState.NEEDS_REVIEW
    assert promoted.metadata["review_renderable"] is True
    assert _task(promoted, "source_setup").status is ReviewStatus.RESOLVED
    assert _task(promoted, "source_setup").accepted_value == "manual_setup_completed"
    assert _task(promoted, "hook").status is ReviewStatus.OPEN
    assert _task(promoted, "crop_confirmation").status is ReviewStatus.OPEN
    assert _task(promoted, "preview_approval").status is ReviewStatus.OPEN
    assert {profile.profile_id for profile in promoted.output_profiles} >= {
        "shorts_preview",
        "shorts_final",
    }

    promoted = _resolve_blockers(service, promoted)
    preview = service.render_preview(promoted.project_id)
    ready = service.approve_preview(promoted.project_id, str(preview["job_id"]))
    assert ready.state is ProjectState.READY

    # The first-pass P1 fix remains intact: historical Inbox/bootstrap retries cannot
    # rewind an already approved project.
    same_ready = service.bootstrap_project(ready.project_id)
    assert same_ready.state is ProjectState.READY
    assert _task(same_ready, "preview_approval").status is ReviewStatus.RESOLVED
