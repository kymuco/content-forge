"""Seventh/ninth-pass PR10 hardening for final receipts and bulk preparation."""

from __future__ import annotations

import hashlib
import json

from content_forge.core import AttentionMode, ProjectState, ReviewStatus
from content_forge.orchestration import RenderJobIntegrityError
from content_forge.timeline import render_plan_digest

from . import review as _review


_MANUAL_SETUP_FINGERPRINT = "pr10_manual_setup_input_fingerprint"
_FINAL_RECEIPT_KEYS = (
    "final_render_job_id",
    "final_render_plan_digest",
    "final_output_sha256",
)
_APPROVED_PREVIEW_KEYS = (
    "approved_preview_job_id",
    "approved_preview_plan_digest",
    "approved_preview_revision_digest",
)


class ReviewService(_review.ReviewService):
    """Close final-receipt, recovery, and complete-project preparation gaps."""

    def _manual_setup_input_fingerprint(self, project) -> str:
        """Digest only canonical inputs that can change PR10 setup/renderability."""

        asset_ids = {ref.asset_id for ref in project.source_refs}
        for scene in project.scenes:
            if scene.media is not None:
                asset_ids.add(scene.media.asset_id)
        assets: dict[str, object] = {}
        for asset_id in sorted(asset_ids):
            asset = self.library.database.get_asset(asset_id)
            assets[asset_id] = (
                None if asset is None else asset.model_dump(mode="json")
            )
        payload = {
            "source_refs": [ref.model_dump(mode="json") for ref in project.source_refs],
            "template": (
                None if project.template is None else project.template.model_dump(mode="json")
            ),
            "scenes": [scene.model_dump(mode="json") for scene in project.scenes],
            "output_profiles": [
                profile.model_dump(mode="json") for profile in project.output_profiles
            ],
            "assets": assets,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _sync_manual_setup_fingerprint(self, project_id: str):
        """Record the last checked setup inputs without issuing a MANUAL receipt."""

        def sync(current):
            if current.metadata.get("pr10_manual_reentry_pending") is True:
                return current
            initialized = bool(current.metadata.get("pr10_review_initialized"))
            if not initialized:
                return current
            metadata = dict(current.metadata)
            if bool(current.metadata.get("review_renderable")):
                if metadata.pop(_MANUAL_SETUP_FINGERPRINT, None) is None:
                    return current
            else:
                fingerprint = self._manual_setup_input_fingerprint(current)
                if metadata.get(_MANUAL_SETUP_FINGERPRINT) == fingerprint:
                    return current
                metadata[_MANUAL_SETUP_FINGERPRINT] = fingerprint
            return current.validated_copy(
                update={"metadata": metadata, "updated_at": _review._base._utc_now()}
            )

        return self._mutate_project(project_id, sync)

    def _manual_setup_inputs_unchanged(self, project) -> bool:
        """Make direct bootstrap idempotent for an already-checked MANUAL snapshot."""

        if (
            project.metadata.get("pr10_manual_reentry_pending") is True
            or not bool(project.metadata.get("pr10_review_initialized"))
            or bool(project.metadata.get("review_renderable"))
            or project.state not in self._MANUAL_RECHECK_STATES
        ):
            return False
        expected = project.metadata.get(_MANUAL_SETUP_FINGERPRINT)
        if not isinstance(expected, str) or not expected:
            return False
        return expected == self._manual_setup_input_fingerprint(project)

    def bootstrap_project(self, project_id: str):
        """Bootstrap/recheck only when the canonical manual-setup inputs changed."""

        current = self.get_project(project_id)
        if self._manual_setup_inputs_unchanged(current):
            return current
        prepared = super().bootstrap_project(project_id)
        return self._sync_manual_setup_fingerprint(prepared.project_id)

    def _current_final_plan_digest(self, project) -> str | None:
        """Compile today's canonical final semantics without trusting persisted receipts."""

        try:
            current_plan = self._compile_plan(
                project,
                _review._base.SHORTS_FINAL_PROFILE_ID,
            )
        except (_review.ReviewError, TypeError, ValueError):
            return None
        return render_plan_digest(current_plan)

    @staticmethod
    def _has_complete_final_receipt(project) -> bool:
        return all(
            isinstance(project.metadata.get(key), str)
            and bool(project.metadata.get(key))
            for key in _FINAL_RECEIPT_KEYS
        )

    def _final_receipt_is_semantically_stale(self, project) -> bool:
        """Classify any retained final-plan digest before artifact completeness."""

        expected_digest = project.metadata.get("final_render_plan_digest")
        if not isinstance(expected_digest, str) or not expected_digest:
            return False
        current_digest = self._current_final_plan_digest(project)
        return current_digest is None or current_digest != expected_digest

    def _active_final_claim_is_semantically_stale(self, project) -> bool:
        expected_digest = project.metadata.get("active_final_plan_digest")
        if not isinstance(expected_digest, str) or not expected_digest:
            return False
        current_digest = self._current_final_plan_digest(project)
        return current_digest is None or current_digest != expected_digest

    def _approved_preview_identity_is_current(self, project) -> bool:
        """Prove that READY recovery still has the exact explicit preview approval."""

        job_id = project.metadata.get("approved_preview_job_id")
        digest = project.metadata.get("approved_preview_plan_digest")
        revision = project.metadata.get("approved_preview_revision_digest")
        if not all(
            isinstance(value, str) and bool(value)
            for value in (job_id, digest, revision)
        ):
            return False

        preview = self._task(project, _review._base._PREVIEW_TASK)
        if (
            preview is None
            or preview.status is not ReviewStatus.RESOLVED
            or preview.accepted_value != job_id
        ):
            return False

        # Approval is recorded in READY. RENDERING/QC/DONE are lifecycle-only changes and
        # final receipt fields are already normalized out of the revision digest, so project
        # those states back to READY before comparing the durable approval revision.
        revision_project = (
            project
            if project.state is ProjectState.READY
            else project.validated_copy(update={"state": ProjectState.READY})
        )
        if _review._base._preview_revision_digest(revision_project) != revision:
            return False

        try:
            current_preview = self._compile_plan(
                project,
                _review._base.SHORTS_PREVIEW_PROFILE_ID,
            )
        except (_review.ReviewError, TypeError, ValueError):
            return False
        return render_plan_digest(current_preview) == digest

    def _reopen_stale_final(self, project_id: str, *, reason: str):
        """Return stale final state to an actionable canonical review lifecycle."""

        def reopen(current):
            tasks = []
            for task in current.review_tasks:
                replacement = task
                if task.task_type == _review._base._PREVIEW_TASK:
                    replacement = task.validated_copy(
                        update={
                            "status": ReviewStatus.OPEN,
                            "accepted_value": None,
                            "resolved_at": None,
                            "payload": {"status": "not_rendered"},
                        }
                    )
                elif (
                    task.attention is AttentionMode.REVIEW
                    and task.task_type in _review._base._EDIT_TASKS
                ):
                    try:
                        payload = self._canonical_edit_payload(current, task.task_type)
                    except _review.ReviewError:
                        payload = dict(task.payload)
                    replacement = task.validated_copy(
                        update={
                            "status": ReviewStatus.OPEN,
                            "accepted_value": None,
                            "resolved_at": None,
                            "payload": payload,
                        }
                    )
                tasks.append(replacement)

            metadata = dict(current.metadata)
            for key in (*_FINAL_RECEIPT_KEYS, *_APPROVED_PREVIEW_KEYS):
                metadata.pop(key, None)
            metadata.pop("active_final_plan_digest", None)
            metadata["last_final_render_error"] = reason[:1024]
            return current.validated_copy(
                update={
                    "state": ProjectState.NEEDS_REVIEW,
                    "review_tasks": tuple(tasks),
                    "metadata": metadata,
                    "updated_at": _review._base._utc_now(),
                }
            )

        return self._mutate_project(project_id, reopen)

    def _validated_final_artifact(self, project):
        """Accept a final artifact only for the complete current semantic final plan."""

        job_id = project.metadata.get("final_render_job_id")
        expected_digest = project.metadata.get("final_render_plan_digest")
        expected_sha = project.metadata.get("final_output_sha256")
        if not all(
            isinstance(value, str) and bool(value)
            for value in (job_id, expected_digest, expected_sha)
        ):
            return None

        current_digest = self._current_final_plan_digest(project)
        if current_digest is None or current_digest != expected_digest:
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

    def _recover_failed_final_qc(self, project_id: str, exc: BaseException) -> None:
        """Make a failed post-render QC transition actionable in the same request."""

        project = self.get_project(project_id)
        if project.state is not ProjectState.QC:
            return
        if self._final_receipt_is_semantically_stale(project):
            self._reopen_stale_final(
                project_id,
                reason="stale final QC failure returned project to review",
            )
            return
        if not self._approved_preview_identity_is_current(project):
            self._reopen_stale_final(
                project_id,
                reason="final QC failure lost current approved preview; returned project to review",
            )
            return

        detail = str(exc)[:1024] or type(exc).__name__

        def recover(current):
            if current.state is not ProjectState.QC:
                return current
            metadata = dict(current.metadata)
            for key in _FINAL_RECEIPT_KEYS:
                metadata.pop(key, None)
            metadata.pop("active_final_plan_digest", None)
            metadata["last_final_render_error"] = detail
            return current.validated_copy(
                update={
                    "state": ProjectState.READY,
                    "metadata": metadata,
                    "updated_at": _review._base._utc_now(),
                }
            )

        self._mutate_project(project_id, recover)

    def _record_final_success(self, project_id: str, artifact, final_digest: str):
        """Never leave a request stranded in QC when post-render validation fails."""

        try:
            return super()._record_final_success(project_id, artifact, final_digest)
        except BaseException as exc:
            try:
                self._recover_failed_final_qc(project_id, exc)
            except Exception:
                pass
            raise

    def render_final(self, project_id: str) -> dict[str, object]:
        """Never strand final recovery without a current explicit preview approval."""

        project = self.get_project(project_id)
        if (
            project.state is ProjectState.DONE
            and self._final_receipt_is_semantically_stale(project)
        ):
            self._reopen_stale_final(
                project_id,
                reason="stale final receipt returned project to review",
            )
            raise _review.ReviewConflictError(
                "final receipt is stale; project returned to review"
            )

        if project.state is ProjectState.DONE and not self._approved_preview_identity_is_current(
            project
        ):
            artifact = self._validated_final_artifact(project)
            if artifact is None:
                self._reopen_stale_final(
                    project_id,
                    reason=(
                        "unrecoverable final receipt lost current approved preview; "
                        "returned project to review"
                    ),
                )
                raise _review.ReviewConflictError(
                    "final recovery lost approved preview; project returned to review"
                )

        if (
            project.state is ProjectState.READY
            and not self._approved_preview_identity_is_current(project)
        ):
            self._reopen_stale_final(
                project_id,
                reason="ready project lost current approved preview; returned project to review",
            )
            raise _review.ReviewConflictError(
                "approved preview is unavailable; project returned to review"
            )

        return super().render_final(project_id)

    def _recover_project_after_restart(self, project) -> None:
        """Reject stale semantic final claims before any QC/adoption transition."""

        if (
            project.state is ProjectState.QC
            and self._final_receipt_is_semantically_stale(project)
        ):
            self._reopen_stale_final(
                project.project_id,
                reason="stale final QC receipt returned project to review",
            )
            return

        if (
            project.state is ProjectState.QC
            and not self._approved_preview_identity_is_current(project)
            and self._validated_final_artifact(project) is None
        ):
            self._reopen_stale_final(
                project.project_id,
                reason=(
                    "incomplete final QC recovery lost current approved preview; "
                    "returned project to review"
                ),
            )
            return

        if (
            project.state is ProjectState.RENDERING
            and self._active_final_claim_is_semantically_stale(project)
        ):
            digest = project.metadata.get("active_final_plan_digest")
            if isinstance(digest, str):
                for job in self._matching_jobs(
                    project.project_id,
                    purpose="final",
                    plan_digest=digest,
                    states=("running",),
                ):
                    try:
                        _review._base.transition_job_state(
                            self.library.database,
                            job.job_id,
                            expected_state="running",
                            state="failed",
                        )
                    except _review._base.StorageConflictError:
                        pass
            self._reopen_stale_final(
                project.project_id,
                reason="stale active final claim returned project to review",
            )
            return

        return super()._recover_project_after_restart(project)

    @staticmethod
    def _record_bulk_failure(
        failures: list[dict[str, str]],
        project_id: str,
        exc: Exception,
    ) -> None:
        if len(failures) < 20:
            failures.append(
                {
                    "project_id": project_id,
                    "detail": str(exc)[:512] or type(exc).__name__,
                }
            )

    def prepare_inbox_projects(self) -> dict[str, object]:
        """Prepare every safe Project while quarantining per-project read failures."""

        projects = self._list_projects()
        eligible = []
        eligible_count = 0
        failed = 0
        failures: list[dict[str, str]] = []
        for project in projects:
            if project.state not in {ProjectState.INBOX, ProjectState.NEEDS_REVIEW}:
                continue
            initialized = bool(project.metadata.get("pr10_review_initialized"))
            pending = project.metadata.get("pr10_manual_reentry_pending") is True
            renderable = bool(project.metadata.get("review_renderable"))
            if pending or not initialized:
                eligible_count += 1
                eligible.append(project)
                continue
            if renderable:
                continue
            try:
                current_fingerprint = self._manual_setup_input_fingerprint(project)
            except Exception as exc:
                eligible_count += 1
                failed += 1
                self._record_bulk_failure(failures, project.project_id, exc)
                continue
            if project.metadata.get(_MANUAL_SETUP_FINGERPRINT) != current_fingerprint:
                eligible_count += 1
                eligible.append(project)

        processed = 0
        changed = 0
        for project in eligible:
            try:
                before = self.get_project(project.project_id)
                after = self.bootstrap_project(project.project_id)
            except (_review.ReviewError, TypeError, ValueError) as exc:
                failed += 1
                self._record_bulk_failure(failures, project.project_id, exc)
                continue
            processed += 1
            if after != before:
                changed += 1

        return {
            "eligible": eligible_count,
            "processed": processed,
            "changed": changed,
            "failed": failed,
            "failures": failures,
        }


# ``content_forge.application`` is imported before callers can import its ``review``
# submodule directly. Replace the public facade attribute so both import surfaces resolve
# to this same hardened class rather than creating a bypass around these fixes.
_review.ReviewService = ReviewService


__all__ = ["ReviewService"]
