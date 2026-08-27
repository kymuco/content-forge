"""Public PR10 review service with second-pass lifecycle hardening.

The first implementation remains in ``_review_base``; this facade layers the two
post-review invariants that must operate on canonical Project state without changing
PR7 render authority or the phone's bounded edit surface.
"""

from __future__ import annotations

from pydantic import JsonValue

from content_forge.core import AttentionMode, Project, ProjectState, ReviewStatus

from . import _review_base as _base

ReviewConflictError = _base.ReviewConflictError
ReviewError = _base.ReviewError
ReviewNotFoundError = _base.ReviewNotFoundError
ReviewNotReadyError = _base.ReviewNotReadyError
ReviewRenderError = _base.ReviewRenderError
ReviewValidationError = _base.ReviewValidationError


class ReviewService(_base.ReviewService):
    """PR10 review service with canonical task rehydration and manual re-entry."""

    _MANUAL_RECHECK_STATES = frozenset({ProjectState.INBOX, ProjectState.NEEDS_REVIEW})

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
            return {"scene_ids": [scene.scene_id for scene in ordered_scenes]}
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

    def bootstrap_project(self, project_id: str) -> Project:
        """Keep approved lifecycle stable while permitting MANUAL setup re-entry.

        A project that was already renderable remains a strict bootstrap no-op once PR10
        owns it. The one exception is an initialized, non-renderable project still in an
        editable pre-render state: desktop/manual work may have repaired its canonical
        sources/scenes/template, so clear only the initialization fence, let the base
        bootstrap re-evaluate current authority, then retire ``source_setup`` if the
        project became renderable.
        """

        current = self.get_project(project_id)
        should_recheck = (
            bool(current.metadata.get("pr10_review_initialized"))
            and not bool(current.metadata.get("review_renderable"))
            and current.state in self._MANUAL_RECHECK_STATES
        )
        if should_recheck:
            def permit_recheck(project: Project) -> Project:
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
        if (
            prepared.state is not ProjectState.NEEDS_REVIEW
            or not bool(prepared.metadata.get("review_renderable"))
        ):
            return prepared

        def finalize_manual_reentry(project: Project) -> Project:
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
                if task.task_type == _base._AUTO_BOOTSTRAP_TASK:
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
                elif task.task_type == "source_setup" and task.status is ReviewStatus.OPEN:
                    payload = dict(task.payload)
                    payload["status"] = "completed"
                    replacement = task.validated_copy(
                        update={
                            "status": ReviewStatus.RESOLVED,
                            "accepted_value": "manual_setup_completed",
                            "resolved_at": now,
                            "payload": payload,
                        }
                    )
                if replacement != task:
                    changed = True
                tasks.append(replacement)

            if not changed:
                return project
            return project.validated_copy(
                update={"review_tasks": tuple(tasks), "updated_at": now}
            )

        return self._mutate_project(project_id, finalize_manual_reentry)

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

        def rehydrate_resolved_edits(project: Project) -> Project:
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
        # The manifest CAS still serializes this with concurrent approve/reject requests.
        self._mutate_project(project_id, rehydrate_resolved_edits)
        return super().reject_preview(project_id, job_id, feedback=feedback)


__all__ = [
    "ReviewConflictError",
    "ReviewError",
    "ReviewNotFoundError",
    "ReviewNotReadyError",
    "ReviewRenderError",
    "ReviewService",
    "ReviewValidationError",
]
