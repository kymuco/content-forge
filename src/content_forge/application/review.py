"""Durable review workflow and proxy-preview orchestration for PR10."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from pydantic import JsonValue

from content_forge.core import (
    AttentionMode,
    MediaType,
    NormalizedRect,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
    ReviewTask,
    Scene,
    TemplateRef,
    Variant,
    dump_json,
    load_json,
)
from content_forge.orchestration import RenderJobIntegrityError, RenderOrchestrator
from content_forge.profiles.shorts import (
    SHORTS_FINAL_PROFILE_ID,
    SHORTS_PREVIEW_PROFILE_ID,
    shorts_final_profile,
    shorts_preview_profile,
)
from content_forge.render.ffmpeg import FFmpegCapabilities, probe_ffmpeg_runtime
from content_forge.storage import LocalLibrary
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    compile_hook_overlay,
)
from content_forge.timeline import RenderPlan, render_plan_digest


class ReviewError(RuntimeError):
    """Base class for PR10 review-workflow failures."""


class ReviewNotFoundError(ReviewError):
    pass


class ReviewConflictError(ReviewError):
    pass


class ReviewNotReadyError(ReviewError):
    pass


class ReviewValidationError(ReviewError):
    pass


class ReviewRenderError(ReviewError):
    pass


_PRIORITY_RANK = {
    ReviewPriority.BLOCKING: 0,
    ReviewPriority.HIGH: 1,
    ReviewPriority.NORMAL: 2,
    ReviewPriority.LOW: 3,
}
_ATTENTION_RANK = {
    AttentionMode.MANUAL: 0,
    AttentionMode.REVIEW: 1,
    AttentionMode.AUTO: 2,
}
_PREVIEW_TASK = "preview_approval"
_AUTO_BOOTSTRAP_TASK = "timeline_bootstrap"
_EDIT_TASKS = frozenset({"hook", "crop_confirmation", "source_order", "metadata"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json(item) for item in value]
    return value


class ReviewService:
    """Own canonical review decisions while reusing PR7 render authority."""

    def __init__(
        self,
        library: LocalLibrary,
        *,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        orchestrator: RenderOrchestrator | None = None,
        capability_loader: Callable[[], FFmpegCapabilities] | None = None,
    ) -> None:
        self.library = library
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.orchestrator = orchestrator or RenderOrchestrator(library)
        self._capability_loader = capability_loader
        self._capabilities_cache: FFmpegCapabilities | None = None

    def _capabilities(self) -> FFmpegCapabilities:
        if self._capabilities_cache is None:
            try:
                self._capabilities_cache = (
                    self._capability_loader()
                    if self._capability_loader is not None
                    else probe_ffmpeg_runtime(
                        self.ffmpeg_path,
                        self.ffprobe_path,
                        test_nvenc=True,
                    )
                )
            except Exception as exc:
                raise ReviewRenderError(f"FFmpeg capability probe failed: {exc}") from exc
        return self._capabilities_cache

    def _project_snapshot(self, project_id: str) -> tuple[Project, str]:
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise ReviewNotFoundError(f"unknown project: {project_id}")
        manifest_json = str(row["manifest_json"])
        return load_json(Project, manifest_json), manifest_json

    def _commit_project(self, project: Project, expected_json: str) -> Project:
        serialized = dump_json(project)
        with self.library.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE projects
                SET content_kind = ?, state = ?, manifest_json = ?, updated_at = ?
                WHERE project_id = ? AND manifest_json = ?
                """,
                (
                    project.content_kind,
                    project.state.value,
                    serialized,
                    project.updated_at.isoformat(),
                    project.project_id,
                    expected_json,
                ),
            ).rowcount
            if changed != 1:
                raise ReviewConflictError(
                    f"project changed concurrently: {project.project_id}"
                )
        return project

    def _mutate_project(
        self,
        project_id: str,
        mutation: Callable[[Project], Project],
        *,
        retries: int = 3,
    ) -> Project:
        last: ReviewConflictError | None = None
        for _ in range(retries):
            current, expected = self._project_snapshot(project_id)
            updated = mutation(current)
            if updated == current:
                return current
            try:
                return self._commit_project(updated, expected)
            except ReviewConflictError as exc:
                last = exc
        raise last or ReviewConflictError(f"project changed concurrently: {project_id}")

    def _list_projects(self) -> tuple[Project, ...]:
        with self.library.database.connection() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM projects ORDER BY updated_at DESC, project_id"
            ).fetchall()
        return tuple(load_json(Project, row["manifest_json"]) for row in rows)

    @staticmethod
    def _task(project: Project, task_type: str) -> ReviewTask | None:
        return next(
            (task for task in project.review_tasks if task.task_type == task_type),
            None,
        )

    @staticmethod
    def _replace_task(project: Project, replacement: ReviewTask) -> tuple[ReviewTask, ...]:
        return tuple(
            replacement if task.review_task_id == replacement.review_task_id else task
            for task in project.review_tasks
        )

    @staticmethod
    def _review_variant(project: Project) -> Variant:
        configured = project.metadata.get("review_variant_id")
        if isinstance(configured, str):
            variant = next(
                (item for item in project.variants if item.variant_id == configured),
                None,
            )
            if variant is not None:
                return variant
        if not project.variants:
            raise ReviewNotReadyError("project has no review variant")
        return project.variants[0]

    @staticmethod
    def _replace_variant(project: Project, replacement: Variant) -> tuple[Variant, ...]:
        return tuple(
            replacement if item.variant_id == replacement.variant_id else item
            for item in project.variants
        )

    @staticmethod
    def _blocking_human_tasks(
        project: Project,
        *,
        exclude_preview: bool = False,
    ) -> tuple[ReviewTask, ...]:
        return tuple(
            task
            for task in project.review_tasks
            if task.status is ReviewStatus.OPEN
            and task.attention is not AttentionMode.AUTO
            and task.blocking
            and (not exclude_preview or task.task_type != _PREVIEW_TASK)
        )

    @staticmethod
    def _new_task(
        project_id: str,
        task_type: str,
        *,
        attention: AttentionMode = AttentionMode.REVIEW,
        priority: ReviewPriority = ReviewPriority.NORMAL,
        blocking: bool = True,
        payload: Mapping[str, JsonValue] | None = None,
        resolved: bool = False,
        accepted_value: JsonValue | None = None,
    ) -> ReviewTask:
        now = _utc_now()
        return ReviewTask(
            project_id=project_id,
            task_type=task_type,
            attention=attention,
            priority=priority,
            blocking=blocking,
            payload={} if payload is None else payload,
            status=ReviewStatus.RESOLVED if resolved else ReviewStatus.OPEN,
            accepted_value=accepted_value,
            resolved_at=now if resolved else None,
            created_at=now,
        )

    def bootstrap_project(self, project_id: str) -> Project:
        """Turn an Inbox project into the bounded PR10 review contract, idempotently."""

        def bootstrap(project: Project) -> Project:
            if project.state in {ProjectState.RENDERING, ProjectState.QC, ProjectState.DONE}:
                raise ReviewConflictError(
                    f"project cannot be bootstrapped from state {project.state.value}"
                )

            tasks = list(project.review_tasks)
            existing_types = {task.task_type for task in tasks}
            metadata = dict(project.metadata)
            variants = list(project.variants)
            scenes = list(project.scenes)
            output_profiles = list(project.output_profiles)
            template = project.template

            if not variants:
                variants.append(Variant())
            review_variant = variants[0]
            metadata["review_variant_id"] = review_variant.variant_id

            visual_refs: list[tuple[object, object]] = []
            unsupported_refs: list[str] = []
            for ref in project.source_refs:
                asset = self.library.database.get_asset(ref.asset_id)
                if asset is None:
                    raise ReviewConflictError(
                        f"project references missing asset: {ref.asset_id}"
                    )
                if asset.media_type in {MediaType.IMAGE, MediaType.VIDEO}:
                    visual_refs.append((ref, asset))
                else:
                    unsupported_refs.append(ref.asset_id)

            renderable = bool(visual_refs)
            if template is None and renderable:
                template = TemplateRef(
                    template_id=HOOK_OVERLAY_TEMPLATE_ID,
                    version=HOOK_OVERLAY_TEMPLATE_VERSION,
                )
            elif template is not None and (
                template.template_id != HOOK_OVERLAY_TEMPLATE_ID
                or template.version != HOOK_OVERLAY_TEMPLATE_VERSION
            ):
                renderable = False

            if not scenes and renderable:
                generated: list[Scene] = []
                for order, (ref, asset) in enumerate(visual_refs):
                    if asset.media_type is MediaType.IMAGE:
                        duration = 5.0
                    else:
                        duration = asset.duration_seconds
                        if duration is None or duration <= 0:
                            renderable = False
                            break
                    generated.append(
                        Scene(order=order, duration_seconds=duration, media=ref)
                    )
                if renderable:
                    scenes = generated

            if scenes:
                for scene in scenes:
                    if scene.media is None:
                        renderable = False
                        break
                    asset = self.library.database.get_asset(scene.media.asset_id)
                    if asset is None or asset.media_type not in {
                        MediaType.IMAGE,
                        MediaType.VIDEO,
                    }:
                        renderable = False
                        break

            def add_task(task: ReviewTask) -> None:
                if task.task_type not in existing_types:
                    tasks.append(task)
                    existing_types.add(task.task_type)

            add_task(
                self._new_task(
                    project.project_id,
                    _AUTO_BOOTSTRAP_TASK,
                    attention=AttentionMode.AUTO,
                    priority=ReviewPriority.LOW,
                    blocking=False,
                    payload={
                        "template_id": HOOK_OVERLAY_TEMPLATE_ID if renderable else None,
                        "preview_profile_id": SHORTS_PREVIEW_PROFILE_ID,
                    },
                    resolved=True,
                    accepted_value="prepared" if renderable else "manual_setup_required",
                )
            )

            if renderable:
                profile_by_id = {profile.profile_id: profile for profile in output_profiles}
                for expected_profile in (shorts_preview_profile(), shorts_final_profile()):
                    stored = profile_by_id.get(expected_profile.profile_id)
                    if stored is None:
                        output_profiles.append(expected_profile)
                    elif stored != expected_profile:
                        raise ReviewConflictError(
                            "existing output profile conflicts with PR10 built-in profile: "
                            f"{expected_profile.profile_id}"
                        )

                add_task(
                    self._new_task(
                        project.project_id,
                        "hook",
                        priority=ReviewPriority.BLOCKING,
                        payload={
                            "variant_id": review_variant.variant_id,
                            "current": review_variant.hook,
                        },
                    )
                )
                add_task(
                    self._new_task(
                        project.project_id,
                        "crop_confirmation",
                        priority=ReviewPriority.HIGH,
                        payload={"scene_ids": [scene.scene_id for scene in scenes]},
                    )
                )
                if len(scenes) > 1:
                    add_task(
                        self._new_task(
                            project.project_id,
                            "source_order",
                            priority=ReviewPriority.HIGH,
                            payload={
                                "scene_ids": [
                                    scene.scene_id
                                    for scene in sorted(scenes, key=lambda item: item.order)
                                ]
                            },
                        )
                    )
                add_task(
                    self._new_task(
                        project.project_id,
                        "metadata",
                        priority=ReviewPriority.NORMAL,
                        blocking=False,
                        payload={
                            "variant_id": review_variant.variant_id,
                            "title": review_variant.title,
                            "description": review_variant.description,
                            "hashtags": list(review_variant.hashtags),
                        },
                    )
                )
                add_task(
                    self._new_task(
                        project.project_id,
                        _PREVIEW_TASK,
                        priority=ReviewPriority.BLOCKING,
                        payload={"status": "not_rendered"},
                    )
                )
            else:
                add_task(
                    self._new_task(
                        project.project_id,
                        "source_setup",
                        attention=AttentionMode.MANUAL,
                        priority=ReviewPriority.BLOCKING,
                        payload={
                            "reason": (
                                "PR10 requires at least one prepared image/video and "
                                "the built-in hook_overlay template"
                            ),
                            "unsupported_asset_ids": unsupported_refs,
                        },
                    )
                )

            metadata["pr10_review_initialized"] = True
            metadata["review_renderable"] = renderable
            return project.validated_copy(
                update={
                    "state": ProjectState.NEEDS_REVIEW,
                    "variants": tuple(variants),
                    "scenes": tuple(scenes),
                    "template": template,
                    "output_profiles": tuple(output_profiles),
                    "review_tasks": tuple(tasks),
                    "metadata": metadata,
                    "updated_at": _utc_now(),
                }
            )

        return self._mutate_project(project_id, bootstrap)

    def list_queue(
        self,
        *,
        limit: int = 100,
        include_auto: bool = False,
    ) -> dict[str, object]:
        if limit < 1 or limit > 500:
            raise ReviewValidationError("limit must be between 1 and 500")
        queue: list[dict[str, object]] = []
        ready_projects: list[dict[str, object]] = []
        for project in self._list_projects():
            if project.state is ProjectState.READY:
                ready_projects.append(self.project_summary(project))
            for task in project.review_tasks:
                if task.status is not ReviewStatus.OPEN:
                    continue
                if not include_auto and task.attention is AttentionMode.AUTO:
                    continue
                queue.append(
                    {
                        "project_id": project.project_id,
                        "project_state": project.state.value,
                        "content_kind": str(project.content_kind),
                        "task": task.model_dump(mode="json"),
                    }
                )
        queue.sort(
            key=lambda item: (
                0 if item["task"]["blocking"] else 1,
                _PRIORITY_RANK[ReviewPriority(item["task"]["priority"])],
                _ATTENTION_RANK[AttentionMode(item["task"]["attention"])],
                item["task"]["created_at"],
                item["project_id"],
                item["task"]["review_task_id"],
            )
        )
        ready_projects.sort(key=lambda item: str(item["project_id"]))
        return {"items": queue[:limit], "ready_projects": ready_projects}

    def get_project(self, project_id: str) -> Project:
        return self._project_snapshot(project_id)[0]

    def project_summary(self, project: Project) -> dict[str, object]:
        preview_task = self._task(project, _PREVIEW_TASK)
        return {
            "project_id": project.project_id,
            "state": project.state.value,
            "content_kind": str(project.content_kind),
            "review_initialized": bool(project.metadata.get("pr10_review_initialized")),
            "review_renderable": bool(project.metadata.get("review_renderable")),
            "open_blocking_tasks": len(self._blocking_human_tasks(project)),
            "preview": None if preview_task is None else _plain_json(preview_task.payload),
            "tasks": [
                task.model_dump(mode="json")
                for task in project.review_tasks
                if task.attention is not AttentionMode.AUTO
            ],
        }

    def _invalidate_preview(
        self,
        project: Project,
        tasks: tuple[ReviewTask, ...],
    ) -> tuple[tuple[ReviewTask, ...], dict[str, object]]:
        updated_tasks = list(tasks)
        preview = next(
            (task for task in updated_tasks if task.task_type == _PREVIEW_TASK),
            None,
        )
        if preview is not None:
            reset = preview.validated_copy(
                update={
                    "status": ReviewStatus.OPEN,
                    "accepted_value": None,
                    "resolved_at": None,
                    "payload": {"status": "not_rendered"},
                }
            )
            updated_tasks = [
                reset if task.review_task_id == preview.review_task_id else task
                for task in updated_tasks
            ]
        metadata = dict(project.metadata)
        metadata.pop("approved_preview_job_id", None)
        metadata.pop("approved_preview_plan_digest", None)
        return tuple(updated_tasks), metadata

    def resolve_task(
        self,
        project_id: str,
        task_id: str,
        value: JsonValue | None,
    ) -> Project:
        def resolve(project: Project) -> Project:
            if project.state in {ProjectState.RENDERING, ProjectState.QC, ProjectState.DONE}:
                raise ReviewConflictError(
                    f"project cannot be edited in state {project.state.value}"
                )
            task = next(
                (item for item in project.review_tasks if item.review_task_id == task_id),
                None,
            )
            if task is None:
                raise ReviewNotFoundError(f"unknown review task: {task_id}")
            if task.task_type == _PREVIEW_TASK:
                raise ReviewValidationError(
                    "preview approval uses the dedicated approve/reject endpoints"
                )
            if task.attention is AttentionMode.AUTO:
                raise ReviewValidationError("AUTO tasks cannot be edited by a client")
            if task.status is not ReviewStatus.OPEN:
                if task.status is ReviewStatus.RESOLVED and task.accepted_value == value:
                    return project
                raise ReviewConflictError("review task is already closed")

            variants = project.variants
            scenes = project.scenes
            if task.task_type == "hook":
                if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                    raise ReviewValidationError("hook must be a non-empty string")
                variant = self._review_variant(project)
                replacement = variant.validated_copy(update={"hook": value.strip()})
                variants = self._replace_variant(project, replacement)
            elif task.task_type == "crop_confirmation":
                if not isinstance(value, dict):
                    raise ReviewValidationError(
                        "crop confirmation must provide a crops object"
                    )
                crops = value.get("crops")
                if not isinstance(crops, dict):
                    raise ReviewValidationError("crop confirmation requires crops")
                scene_ids = {scene.scene_id for scene in scenes}
                if set(crops) != scene_ids:
                    raise ReviewValidationError(
                        "crop confirmation must cover every current scene exactly once"
                    )
                updated_scenes: list[Scene] = []
                for scene in scenes:
                    raw = crops[scene.scene_id]
                    if raw is None:
                        crop = None
                    elif isinstance(raw, dict):
                        try:
                            crop = NormalizedRect.model_validate(raw)
                        except ValueError as exc:
                            raise ReviewValidationError(
                                f"invalid crop for {scene.scene_id}: {exc}"
                            ) from exc
                    else:
                        raise ReviewValidationError(
                            f"crop for {scene.scene_id} must be an object or null"
                        )
                    updated_scenes.append(scene.validated_copy(update={"crop": crop}))
                scenes = tuple(updated_scenes)
            elif task.task_type == "source_order":
                if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value
                ):
                    raise ReviewValidationError("source order must be a list of scene IDs")
                scene_ids = [scene.scene_id for scene in scenes]
                if len(value) != len(scene_ids) or set(value) != set(scene_ids):
                    raise ReviewValidationError(
                        "source order must be an exact permutation of current scene IDs"
                    )
                by_id = {scene.scene_id: scene for scene in scenes}
                scenes = tuple(
                    by_id[scene_id].validated_copy(update={"order": order})
                    for order, scene_id in enumerate(value)
                )
            elif task.task_type == "metadata":
                if not isinstance(value, dict):
                    raise ReviewValidationError("metadata value must be an object")
                allowed = {"title", "description", "hashtags"}
                if set(value).difference(allowed):
                    raise ReviewValidationError("unsupported metadata field")
                update: dict[str, object] = {}
                if "title" in value:
                    title = value["title"]
                    if title is not None and (
                        not isinstance(title, str) or len(title) > 4096
                    ):
                        raise ReviewValidationError("invalid title")
                    update["title"] = title
                if "description" in value:
                    description = value["description"]
                    if description is not None and (
                        not isinstance(description, str) or len(description) > 20000
                    ):
                        raise ReviewValidationError("invalid description")
                    update["description"] = description
                if "hashtags" in value:
                    hashtags = value["hashtags"]
                    if (
                        not isinstance(hashtags, list)
                        or len(hashtags) > 50
                        or not all(
                            isinstance(item, str) and 0 < len(item) <= 128
                            for item in hashtags
                        )
                    ):
                        raise ReviewValidationError("invalid hashtags")
                    update["hashtags"] = tuple(hashtags)
                variant = self._review_variant(project)
                replacement = variant.validated_copy(update=update)
                variants = self._replace_variant(project, replacement)

            resolved = task.validated_copy(
                update={
                    "status": ReviewStatus.RESOLVED,
                    "accepted_value": value,
                    "resolved_at": _utc_now(),
                }
            )
            tasks = self._replace_task(project, resolved)
            tasks, metadata = self._invalidate_preview(project, tasks)
            return project.validated_copy(
                update={
                    "state": ProjectState.NEEDS_REVIEW,
                    "variants": variants,
                    "scenes": scenes,
                    "review_tasks": tasks,
                    "metadata": metadata,
                    "updated_at": _utc_now(),
                }
            )

        return self._mutate_project(project_id, resolve)

    def _compile_plan(self, project: Project, profile_id: str) -> RenderPlan:
        if not bool(project.metadata.get("review_renderable")):
            raise ReviewNotReadyError("project is not renderable by PR10")
        if (
            project.template is None
            or project.template.template_id != HOOK_OVERLAY_TEMPLATE_ID
            or project.template.version != HOOK_OVERLAY_TEMPLATE_VERSION
        ):
            raise ReviewNotReadyError("PR10 currently renders only hook_overlay projects")
        variant = self._review_variant(project)
        if not variant.hook:
            raise ReviewNotReadyError("hook review must be resolved before preview")
        try:
            return compile_hook_overlay(
                project,
                self.library.database,
                profile_id=profile_id,
                variant_id=variant.variant_id,
            )
        except Exception as exc:
            raise ReviewNotReadyError(f"project cannot compile for review: {exc}") from exc

    def _matching_artifact(
        self,
        project_id: str,
        *,
        purpose: str,
        plan_digest: str,
    ):
        with self.library.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT job_id, payload_json FROM jobs
                WHERE project_id = ? AND job_type = 'render' AND state = 'succeeded'
                ORDER BY updated_at DESC, job_id DESC
                """,
                (project_id,),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                payload.get("purpose") == purpose
                and payload.get("render_plan_digest") == plan_digest
            ):
                try:
                    artifact = self.orchestrator.load_artifact(
                        str(row["job_id"]),
                        ffprobe_path=self.ffprobe_path,
                    )
                except RenderJobIntegrityError as exc:
                    raise ReviewRenderError(
                        f"matching render artifact failed integrity validation: {exc}"
                    ) from exc
                if artifact is not None:
                    return artifact
        return None

    @staticmethod
    def _artifact_summary(artifact) -> dict[str, object]:
        return {
            "job_id": artifact.job_id,
            "project_id": artifact.project_id,
            "purpose": artifact.purpose,
            "profile_id": str(artifact.profile_id),
            "render_plan_digest": artifact.render_plan_digest,
            "output_sha256": artifact.output_sha256,
            "width": artifact.width,
            "height": artifact.height,
            "duration_seconds": artifact.duration_seconds,
            "artifact_endpoint": f"render-jobs/{artifact.job_id}/artifact",
        }

    def render_preview(self, project_id: str) -> dict[str, object]:
        project = self.get_project(project_id)
        blockers = self._blocking_human_tasks(project, exclude_preview=True)
        if blockers:
            raise ReviewNotReadyError(
                "blocking review tasks remain before preview: "
                + ", ".join(task.task_type for task in blockers)
            )
        preview_task = self._task(project, _PREVIEW_TASK)
        if preview_task is None or preview_task.status is not ReviewStatus.OPEN:
            raise ReviewNotReadyError("project has no open preview-approval task")

        plan = self._compile_plan(project, SHORTS_PREVIEW_PROFILE_ID)
        digest = render_plan_digest(plan)
        artifact = self._matching_artifact(
            project_id,
            purpose="preview",
            plan_digest=digest,
        )
        if artifact is None:
            try:
                job = self.orchestrator.submit(plan, purpose="preview")
                artifact = self.orchestrator.run_job(job.job_id, self._capabilities())
            except Exception as exc:
                if isinstance(exc, ReviewError):
                    raise
                raise ReviewRenderError(f"preview render failed: {exc}") from exc

        def record(current: Project) -> Project:
            task = self._task(current, _PREVIEW_TASK)
            if task is None or task.status is not ReviewStatus.OPEN:
                raise ReviewConflictError("preview approval task changed during render")
            current_plan = self._compile_plan(current, SHORTS_PREVIEW_PROFILE_ID)
            if render_plan_digest(current_plan) != digest:
                stale = task.validated_copy(update={"payload": {"status": "stale"}})
                return current.validated_copy(
                    update={
                        "state": ProjectState.NEEDS_REVIEW,
                        "review_tasks": self._replace_task(current, stale),
                        "updated_at": _utc_now(),
                    }
                )
            ready = task.validated_copy(
                update={
                    "payload": {
                        "status": "ready",
                        "job_id": artifact.job_id,
                        "render_plan_digest": digest,
                        "output_sha256": artifact.output_sha256,
                        "width": artifact.width,
                        "height": artifact.height,
                    }
                }
            )
            return current.validated_copy(
                update={
                    "state": ProjectState.NEEDS_REVIEW,
                    "review_tasks": self._replace_task(current, ready),
                    "updated_at": _utc_now(),
                }
            )

        recorded = self._mutate_project(project_id, record)
        task = self._task(recorded, _PREVIEW_TASK)
        if task is None or task.payload.get("status") != "ready":
            raise ReviewConflictError(
                "project changed while preview rendered; render a fresh preview"
            )
        return self._artifact_summary(artifact)

    def approve_preview(self, project_id: str, job_id: str) -> Project:
        def approve(project: Project) -> Project:
            task = self._task(project, _PREVIEW_TASK)
            if task is None or task.status is not ReviewStatus.OPEN:
                raise ReviewConflictError("preview approval task is not open")
            if task.payload.get("status") != "ready" or task.payload.get("job_id") != job_id:
                raise ReviewConflictError("preview job is not the current approval candidate")
            expected_digest = task.payload.get("render_plan_digest")
            if not isinstance(expected_digest, str):
                raise ReviewConflictError("preview task has no render-plan digest")
            plan = self._compile_plan(project, SHORTS_PREVIEW_PROFILE_ID)
            current_digest = render_plan_digest(plan)
            if current_digest != expected_digest:
                raise ReviewConflictError("preview is stale for current project state")
            try:
                artifact = self.orchestrator.load_artifact(
                    job_id,
                    ffprobe_path=self.ffprobe_path,
                )
            except RenderJobIntegrityError as exc:
                raise ReviewConflictError(
                    f"preview artifact failed integrity check: {exc}"
                ) from exc
            if (
                artifact is None
                or artifact.project_id != project.project_id
                or artifact.purpose != "preview"
                or artifact.render_plan_digest != current_digest
            ):
                raise ReviewConflictError("preview artifact identity does not match project")
            resolved = task.validated_copy(
                update={
                    "status": ReviewStatus.RESOLVED,
                    "accepted_value": job_id,
                    "resolved_at": _utc_now(),
                }
            )
            tasks = self._replace_task(project, resolved)
            if any(
                item.status is ReviewStatus.OPEN
                and item.attention is not AttentionMode.AUTO
                and item.blocking
                for item in tasks
            ):
                raise ReviewConflictError("blocking review tasks remain")
            metadata = dict(project.metadata)
            metadata["approved_preview_job_id"] = job_id
            metadata["approved_preview_plan_digest"] = current_digest
            return project.validated_copy(
                update={
                    "state": ProjectState.READY,
                    "review_tasks": tasks,
                    "metadata": metadata,
                    "updated_at": _utc_now(),
                }
            )

        return self._mutate_project(project_id, approve)

    def reject_preview(
        self,
        project_id: str,
        job_id: str,
        *,
        feedback: str | None = None,
    ) -> Project:
        if feedback is not None and len(feedback) > 4096:
            raise ReviewValidationError("preview feedback is too long")

        def reject(project: Project) -> Project:
            preview = self._task(project, _PREVIEW_TASK)
            if (
                preview is None
                or preview.status is not ReviewStatus.OPEN
                or preview.payload.get("job_id") != job_id
            ):
                raise ReviewConflictError("preview job is not the current approval candidate")
            reopened: list[ReviewTask] = []
            for task in project.review_tasks:
                if task.review_task_id == preview.review_task_id:
                    payload: dict[str, JsonValue] = {
                        "status": "rejected",
                        "job_id": job_id,
                    }
                    if feedback:
                        payload["feedback"] = feedback
                    reopened.append(
                        task.validated_copy(
                            update={
                                "status": ReviewStatus.OPEN,
                                "accepted_value": None,
                                "resolved_at": None,
                                "payload": payload,
                            }
                        )
                    )
                elif (
                    task.attention is not AttentionMode.AUTO
                    and task.task_type in _EDIT_TASKS
                    and task.status is ReviewStatus.RESOLVED
                ):
                    reopened.append(
                        task.validated_copy(
                            update={
                                "status": ReviewStatus.OPEN,
                                "accepted_value": None,
                                "resolved_at": None,
                            }
                        )
                    )
                else:
                    reopened.append(task)
            metadata = dict(project.metadata)
            metadata.pop("approved_preview_job_id", None)
            metadata.pop("approved_preview_plan_digest", None)
            return project.validated_copy(
                update={
                    "state": ProjectState.NEEDS_REVIEW,
                    "review_tasks": tuple(reopened),
                    "metadata": metadata,
                    "updated_at": _utc_now(),
                }
            )

        return self._mutate_project(project_id, reject)

    def render_final(self, project_id: str) -> dict[str, object]:
        project = self.get_project(project_id)
        if project.state is not ProjectState.READY:
            raise ReviewNotReadyError("project must be ready before final render")
        approved_digest = project.metadata.get("approved_preview_plan_digest")
        if not isinstance(approved_digest, str):
            raise ReviewNotReadyError("project has no approved preview digest")
        current_preview = self._compile_plan(project, SHORTS_PREVIEW_PROFILE_ID)
        if render_plan_digest(current_preview) != approved_digest:
            raise ReviewConflictError("approved preview is stale for current project state")
        final_plan = self._compile_plan(project, SHORTS_FINAL_PROFILE_ID)
        final_digest = render_plan_digest(final_plan)

        def claim(current: Project) -> Project:
            if current.state is not ProjectState.READY:
                raise ReviewConflictError("project is no longer ready for final render")
            approved = current.metadata.get("approved_preview_plan_digest")
            current_preview_plan = self._compile_plan(current, SHORTS_PREVIEW_PROFILE_ID)
            if approved != render_plan_digest(current_preview_plan):
                raise ReviewConflictError("approved preview changed before final render")
            metadata = dict(current.metadata)
            metadata["active_final_plan_digest"] = final_digest
            return current.validated_copy(
                update={
                    "state": ProjectState.RENDERING,
                    "metadata": metadata,
                    "updated_at": _utc_now(),
                }
            )

        self._mutate_project(project_id, claim)
        try:
            artifact = self._matching_artifact(
                project_id,
                purpose="final",
                plan_digest=final_digest,
            )
            if artifact is None:
                job = self.orchestrator.submit(final_plan, purpose="final")
                artifact = self.orchestrator.run_job(job.job_id, self._capabilities())

            def qc(current: Project) -> Project:
                if current.state is not ProjectState.RENDERING:
                    raise ReviewConflictError("project left rendering state unexpectedly")
                metadata = dict(current.metadata)
                metadata["final_render_job_id"] = artifact.job_id
                metadata["final_render_plan_digest"] = final_digest
                metadata["final_output_sha256"] = artifact.output_sha256
                metadata.pop("active_final_plan_digest", None)
                return current.validated_copy(
                    update={
                        "state": ProjectState.QC,
                        "metadata": metadata,
                        "updated_at": _utc_now(),
                    }
                )

            self._mutate_project(project_id, qc)

            def done(current: Project) -> Project:
                if current.state is not ProjectState.QC:
                    raise ReviewConflictError("project left QC state unexpectedly")
                return current.validated_copy(
                    update={"state": ProjectState.DONE, "updated_at": _utc_now()}
                )

            self._mutate_project(project_id, done)
            return self._artifact_summary(artifact)
        except BaseException as exc:
            try:
                def recover(current: Project) -> Project:
                    if current.state is not ProjectState.RENDERING:
                        return current
                    metadata = dict(current.metadata)
                    metadata.pop("active_final_plan_digest", None)
                    metadata["last_final_render_error"] = (
                        str(exc)[:1024] or type(exc).__name__
                    )
                    return current.validated_copy(
                        update={
                            "state": ProjectState.READY,
                            "metadata": metadata,
                            "updated_at": _utc_now(),
                        }
                    )

                self._mutate_project(project_id, recover)
            except Exception:
                pass
            if isinstance(exc, ReviewError):
                raise
            raise ReviewRenderError(f"final render failed: {exc}") from exc

    def artifact_path(self, job_id: str):
        try:
            artifact = self.orchestrator.load_artifact(
                job_id,
                ffprobe_path=self.ffprobe_path,
            )
        except RenderJobIntegrityError as exc:
            raise ReviewConflictError(
                f"render artifact failed integrity check: {exc}"
            ) from exc
        if artifact is None:
            raise ReviewNotFoundError(f"render artifact is not available: {job_id}")
        path = self.library.paths.root / artifact.output_storage_key
        if not path.is_file():
            raise ReviewConflictError("authenticated render artifact file is missing")
        return artifact, path


__all__ = [
    "ReviewConflictError",
    "ReviewError",
    "ReviewNotFoundError",
    "ReviewNotReadyError",
    "ReviewRenderError",
    "ReviewService",
    "ReviewValidationError",
]
