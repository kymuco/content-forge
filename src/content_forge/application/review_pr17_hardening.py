"""PR17 ownership boundary for PR10 startup reconciliation."""

from __future__ import annotations

from content_forge.core import ProjectState, ReviewStatus

from . import _review_base as _base
from .review import ReviewConflictError, ReviewError
from .review_seventh_hardening import ReviewService as _PreviousReviewService


class ReviewService(_PreviousReviewService):
    """Keep batch-owned render attempts out of PR10 startup job reconciliation."""

    def reconcile_persisted_state(self) -> None:
        """Recover PR10 state without stealing PR17-owned render attempts.

        Presence of ``batch_context`` reserves render-attempt lifecycle authority to
        PR17. Even malformed batch context is left untouched here so the batch layer can
        fail closed on its own integrity contract instead of PR10 rewriting the state.
        """

        projects = self._list_projects()
        valid_projects = []
        valid_ids: set[str] = set()
        for project in projects:
            if (
                not bool(project.metadata.get("pr10_review_initialized"))
                or project.metadata.get("pr10_manual_reentry_pending") is True
            ):
                continue
            try:
                self._validate_reserved_task_authority(project)
            except ReviewConflictError:
                continue
            valid_projects.append(project)
            valid_ids.add(project.project_id)

        # Reset only validated PR10 preview claims. This updates Project review state,
        # not render-attempt ownership, so it remains valid for projects that also have
        # independent PR17 batch work.
        for project in valid_projects:
            preview = self._task(project, _base._PREVIEW_TASK)
            if (
                preview is not None
                and preview.status is ReviewStatus.OPEN
                and preview.payload.get("status") == "rendering"
            ):

                def reset_preview(current):
                    self._require_initialized_authority(current)
                    task = self._task(current, _base._PREVIEW_TASK)
                    if (
                        task is None
                        or task.status is not ReviewStatus.OPEN
                        or task.payload.get("status") != "rendering"
                    ):
                        return current
                    reset = task.validated_copy(
                        update={"payload": {"status": "not_rendered"}}
                    )
                    return current.validated_copy(
                        update={
                            "review_tasks": self._replace_task(current, reset),
                            "updated_at": _base._utc_now(),
                        }
                    )

                try:
                    self._mutate_project(project.project_id, reset_preview)
                except ReviewConflictError:
                    valid_ids.discard(project.project_id)

        # PR10 may retire only its own orphaned preview attempts. A row carrying
        # `batch_context` belongs to PR17, whose lease + frozen-plan recovery protocol
        # supplies authenticated interruption evidence before any retry.
        with self.library.database.connection() as connection:
            rows = connection.execute(
                "SELECT job_id, project_id, payload_json FROM jobs WHERE state = 'running'"
            ).fetchall()
        for row in rows:
            project_id = str(row["project_id"])
            if project_id not in valid_ids:
                continue
            try:
                payload = _base.json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("purpose") != "preview":
                continue
            if "batch_context" in payload:
                continue
            try:
                _base.transition_job_state(
                    self.library.database,
                    str(row["job_id"]),
                    expected_state="running",
                    state="failed",
                )
            except (_base.StorageConflictError, TypeError, ValueError):
                continue

        for project in self._list_projects():
            if (
                project.project_id in valid_ids
                and project.state in {ProjectState.RENDERING, ProjectState.QC}
            ):
                try:
                    self._require_initialized_authority(project)
                    self._recover_project_after_restart(project)
                except (ReviewError, _base.StorageConflictError, TypeError, ValueError):
                    continue


__all__ = ["ReviewService"]
