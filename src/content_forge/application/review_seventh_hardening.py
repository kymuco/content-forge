"""Seventh-pass PR10 hardening for final receipts and bulk review preparation."""

from __future__ import annotations

from content_forge.core import ProjectState
from content_forge.orchestration import RenderJobIntegrityError

from . import review as _review


class ReviewService(_review.ReviewService):
    """Close seventh-pass recovery and Inbox-enumeration gaps."""

    def _validated_final_artifact(self, project):
        """Accept a final artifact only with a complete canonical QC receipt."""

        job_id = project.metadata.get("final_render_job_id")
        expected_digest = project.metadata.get("final_render_plan_digest")
        expected_sha = project.metadata.get("final_output_sha256")
        if not all(
            isinstance(value, str) and bool(value)
            for value in (job_id, expected_digest, expected_sha)
        ):
            return None
        try:
            artifact = self.orchestrator.load_artifact(
                job_id,
                ffprobe_path=self.ffprobe_path,
            )
        except RenderJobIntegrityError:
            return None
        if artifact is None:
            return None
        if (
            artifact.job_id != job_id
            or artifact.project_id != project.project_id
            or artifact.purpose != "final"
            or artifact.render_plan_digest != expected_digest
            or artifact.output_sha256 != expected_sha
        ):
            return None
        return artifact

    def prepare_inbox_projects(self) -> dict[str, object]:
        """Prepare every safe eligible Project without depending on intake pagination."""

        projects = self._list_projects()
        eligible = []
        for project in projects:
            if project.state not in {ProjectState.INBOX, ProjectState.NEEDS_REVIEW}:
                continue
            initialized = bool(project.metadata.get("pr10_review_initialized"))
            pending = project.metadata.get("pr10_manual_reentry_pending") is True
            renderable = bool(project.metadata.get("review_renderable"))
            if pending or not initialized or not renderable:
                eligible.append(project)

        processed = 0
        changed = 0
        failed = 0
        failures: list[dict[str, str]] = []
        for project in eligible:
            try:
                before = self.get_project(project.project_id)
                after = self.bootstrap_project(project.project_id)
            except (_review.ReviewError, TypeError, ValueError) as exc:
                failed += 1
                if len(failures) < 20:
                    failures.append(
                        {
                            "project_id": project.project_id,
                            "detail": str(exc)[:512] or type(exc).__name__,
                        }
                    )
                continue
            processed += 1
            if after != before:
                changed += 1

        return {
            "eligible": len(eligible),
            "processed": processed,
            "changed": changed,
            "failed": failed,
            "failures": failures,
        }


# ``content_forge.application`` is imported before callers can import its ``review``
# submodule directly. Replace the public facade attribute so both import surfaces resolve
# to this same hardened class rather than creating a bypass around the seventh-pass fixes.
_review.ReviewService = ReviewService


__all__ = ["ReviewService"]
