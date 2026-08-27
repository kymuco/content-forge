"""Public PR10 review service with adversarial-review hardening.

The first implementation remains in ``_review_base``; this facade owns the hardened
public authority checks and lifecycle invariants without changing PR7 render authority
or granting the phone a generic Project mutation surface.
"""

from __future__ import annotations

from pydantic import JsonValue

from content_forge.core import (
    AttentionMode,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
)

from . import _review_base as _base

ReviewConflictError = _base.ReviewConflictError
ReviewError = _base.ReviewError
ReviewNotFoundError = _base.ReviewNotFoundError
ReviewNotReadyError = _base.ReviewNotReadyError
ReviewRenderError = _base.ReviewRenderError
ReviewValidationError = _base.ReviewValidationError


_TASK_AUTHORITY = {
    _base._AUTO_BOOTSTRAP_TASK: (AttentionMode.AUTO, ReviewPriority.LOW, False),
    "hook": (AttentionMode.REVIEW, ReviewPriority.BLOCKING, True),
    "crop_confirmation": (AttentionMode.REVIEW, ReviewPriority.HIGH, True),
    "source_order": (AttentionMode.REVIEW, ReviewPriority.HIGH, True),
    "metadata": (AttentionMode.REVIEW, ReviewPriority.NORMAL, False),
    _base._PREVIEW_TASK: (AttentionMode.REVIEW, ReviewPriority.BLOCKING, True),
    "source_setup": (AttentionMode.MANUAL, ReviewPriority.BLOCKING, True),
}
_BOOTSTRAP_FORBIDDEN_UNINITIALIZED = frozenset(
    {ProjectState.READY, ProjectState.RENDERING, ProjectState.QC, ProjectState.DONE}
)


class ReviewService(_base.ReviewService):
    """PR10 review service with closed task authority and recoverable manual re-entry."""

    _MANUAL_RECHECK_STATES = frozenset({ProjectState.INBOX, ProjectState.NEEDS_REVIEW})

    @staticmethod
    def _validate_reserved_task_authority(project: Project) -> None:
        """Fail closed on duplicate or authority-mismatched PR10-reserved task types."""

        seen: set[str] = set()
        for task in project.review_tasks:
            expected = _TASK_AUTHORITY.get(task.task_type)
            if expected is None:
                continue
            if task.task_type in seen:
                raise ReviewConflictError(
                    f"duplicate reserved review task type: {task.task_type}"
                )
            seen.add(task.task_type)
            attention, priority, blocking = expected
            if (
                task.project_id != project.project_id
                or task.attention is not attention
                or task.priority is not priority
                or task.blocking is not blocking
            ):
                raise ReviewConflictError(
                    f"reserved review task authority collision: {task.task_type}"
                )

    @staticmethod
    def _reject_uninitialized_reserved_tasks(project: Project) -> None:
        """Never adopt pre-existing state from PR10's reserved task namespace."""

        reserved = sorted(
            task.task_type
            for task in project.review_tasks
            if task.task_type in _TASK_AUTHORITY
        )
        if reserved:
            raise ReviewConflictError(
                "uninitialized project already contains reserved review task: "
                + ", ".join(reserved)
            )

    def _canonical_edit_payload(
        self,
        project: Project,
        task_type: str,
    ) -> dict[str, JsonValue]:
        ordered_scenes = sorted(project.scenes, key=lambda item: item.order)
        if task_type == "hook":
            variant = self._review_variant(project)
            return {"variant_id": variant.variant_id, "current": variant.hook}
        if task_type == "crop_confirmation":
            return {
                "scene_ids": [scene.scene_id for scene in ordered_scenes],
                "crops": {
                    scene.scene_id: (
                        None if scene.crop is None else scene.crop.model_dump(mode="json")
                    )
                    for scene in ordered_scenes
                },
            }
        if task_type == "source_order":
            return {"scene_ids": [scene.scene_id for scene in ordered_scenes]}
        if task_type == "metadata":
            variant = self._review_variant(project)
            return {
                "variant_id": variant.variant_id,
                "title": variant.title,
                "description": variant.description,
                "hashtags": list(variant.hashtags),
            }
        raise ReviewValidationError(f"unsupported editable review task: {task_type}")

    def _finalize_bootstrap_payloads(
        self,
        project_id: str,
        *,
        manual_reentry: bool,
    ) -> Project:
        """Normalize phone payloads and, after successful manual repair, retire MANUAL setup."""

        def finalize(project: Project) -> Project:
            self._validate_reserved_task_authority(project)
            if (
                project.state is not ProjectState.NEEDS_REVIEW
                or not bool(project.metadata.get("review_renderable"))
            ):
                return project

            now = _base._utc_now()
            changed = False
            tasks = []
            expected_auto_payload = {
                "template_id": _base.HOOK_OVERLAY_TEMPLATE_ID,
                "preview_profile_id": _base.SHORTS_PREVIEW_PROFILE_ID,
            }
            for task in project.review_tasks:
                replacement = task
                if manual_reentry and task.task_type == _base._AUTO_BOOTSTRAP_TASK:
                    if (
                        task.status is not ReviewStatus.RESOLVED
                        or task.accepted_value != "prepared"
                        or dict(task.payload) != expected_auto_payload
                    ):
                        replacement = task.validated_copy(
                            update={
                                "status": ReviewStatus.RESOLVED,
                                "accepted_value": "prepared",
                                "resolved_at": now,
                                "payload": expected_auto_payload,
                            }
                        )
                elif (
                    manual_reentry
                    and task.task_type == "source_setup"
                    and task.status is ReviewStatus.OPEN
                ):
                    payload = {
                        str(key): _base._plain_json(value)
                        for key, value in task.payload.items()
                    }
                    payload["status"] = "completed"
                    replacement = task.validated_copy(
                        update={
                            "status": ReviewStatus.RESOLVED,
                            "accepted_value": "manual_setup_completed",
                            "resolved_at": now,
                            "payload": payload,
                        }
                    )
                elif (
                    task.attention is AttentionMode.REVIEW
                    and task.task_type in _base._EDIT_TASKS
                    and task.status is ReviewStatus.OPEN
                ):
                    payload = self._canonical_edit_payload(project, task.task_type)
                    if dict(task.payload) != payload:
                        replacement = task.validated_copy(update={"payload": payload})

                if replacement != task:
                    changed = True
                tasks.append(replacement)

            if not changed:
                return project
            return project.validated_copy(
                update={"review_tasks": tuple(tasks), "updated_at": now}
            )

        return self._mutate_project(project_id, finalize)

    def bootstrap_project(self, project_id: str) -> Project:
        """Bootstrap only editable lifecycle states and safely re-evaluate MANUAL repairs."""

        current = self.get_project(project_id)
        initialized = bool(current.metadata.get("pr10_review_initialized"))

        # Before PR10 has established its initialization receipt it owns none of the
        # reserved task namespace. Even a structurally matching task may carry arbitrary
        # status/payload/accepted-value lifecycle state, so adopting it by type is unsafe.
        if not initialized:
            self._reject_uninitialized_reserved_tasks(current)
        else:
            self._validate_reserved_task_authority(current)

        # READY is intentionally editable for optional phone metadata, but it is never a
        # legal bootstrap input. An older workflow's uninitialized READY project must not
        # be rewound into PR10 review just because it appears in historical Inbox data.
        if not initialized and current.state in _BOOTSTRAP_FORBIDDEN_UNINITIALIZED:
            raise ReviewConflictError(
                f"project cannot be bootstrapped from state {current.state.value}"
            )

        should_recheck = (
            initialized
            and not bool(current.metadata.get("review_renderable"))
            and current.state in self._MANUAL_RECHECK_STATES
        )
        if initialized and not should_recheck:
            return current

        if should_recheck:
            def permit_recheck(project: Project) -> Project:
                self._validate_reserved_task_authority(project)
                if (
                    not bool(project.metadata.get("pr10_review_initialized"))
                    or bool(project.metadata.get("review_renderable"))
                    or project.state not in self._MANUAL_RECHECK_STATES
                ):
                    return project
                metadata = dict(project.metadata)
                metadata.pop("pr10_review_initialized", None)
                return project.validated_copy(
                    update={"metadata": metadata, "updated_at": _base._utc_now()}
                )

            self._mutate_project(project_id, permit_recheck)

        prepared = super().bootstrap_project(project_id)
        self._validate_reserved_task_authority(prepared)
        return self._finalize_bootstrap_payloads(
            project_id,
            manual_reentry=should_recheck,
        )

    def resolve_task(
        self,
        project_id: str,
        task_id: str,
        value: JsonValue | None,
    ) -> Project:
        self._validate_reserved_task_authority(self.get_project(project_id))
        return super().resolve_task(project_id, task_id, value)

    def render_preview(self, project_id: str) -> dict[str, object]:
        self._validate_reserved_task_authority(self.get_project(project_id))
        return super().render_preview(project_id)

    def approve_preview(self, project_id: str, job_id: str) -> Project:
        self._validate_reserved_task_authority(self.get_project(project_id))
        return super().approve_preview(project_id, job_id)

    def reject_preview(
        self,
        project_id: str,
        job_id: str,
        *,
        feedback: str | None = None,
    ) -> Project:
        """Reject a preview while reopening edit cards from canonical current values."""

        if feedback is not None and len(feedback) > 4096:
            raise ReviewValidationError("preview feedback is too long")
        self._validate_reserved_task_authority(self.get_project(project_id))

        def rehydrate_resolved_edits(project: Project) -> Project:
            self._validate_reserved_task_authority(project)
            preview = self._task(project, _base._PREVIEW_TASK)
            if (
                preview is None
                or preview.status is not ReviewStatus.OPEN
                or preview.payload.get("job_id") != job_id
            ):
                raise ReviewConflictError("preview job is not the current approval candidate")

            changed = False
            tasks = []
            for task in project.review_tasks:
                replacement = task
                if (
                    task.attention is AttentionMode.REVIEW
                    and task.task_type in _base._EDIT_TASKS
                    and task.status is ReviewStatus.RESOLVED
                ):
                    payload = self._canonical_edit_payload(project, task.task_type)
                    if dict(task.payload) != payload:
                        replacement = task.validated_copy(update={"payload": payload})
                if replacement != task:
                    changed = True
                tasks.append(replacement)

            if not changed:
                return project
            return project.validated_copy(
                update={"review_tasks": tuple(tasks), "updated_at": _base._utc_now()}
            )

        # Resolved tasks are not visible/editable in the phone queue, so refreshing their
        # payload before the rejection commit cannot expose an intermediate stale form.
        # The project manifest CAS serializes this with competing phone mutations.
        self._mutate_project(project_id, rehydrate_resolved_edits)
        return super().reject_preview(project_id, job_id, feedback=feedback)

    def render_final(self, project_id: str) -> dict[str, object]:
        self._validate_reserved_task_authority(self.get_project(project_id))
        return super().render_final(project_id)

    def reconcile_persisted_state(self) -> None:
        """Validate facade-owned authority before any restart recovery mutation."""

        # The API invokes this only after the exclusive RuntimeLease is held. Validate the
        # entire initialized PR10 set first so inherited recovery cannot reset a foreign or
        # ambiguous preview task, retire jobs, or transition project state before the
        # hardened facade has established authority over every affected manifest.
        projects = self._list_projects()
        for project in projects:
            if bool(project.metadata.get("pr10_review_initialized")):
                self._validate_reserved_task_authority(project)
        super().reconcile_persisted_state()


__all__ = [
    "ReviewConflictError",
    "ReviewError",
    "ReviewNotFoundError",
    "ReviewNotReadyError",
    "ReviewRenderError",
    "ReviewService",
    "ReviewValidationError",
]
