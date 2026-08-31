"""PR17 recovery hardening that clones retries from frozen PR7 plan evidence."""

from __future__ import annotations

import shutil

from content_forge.storage import StoredJob
from content_forge.timeline import RenderPlan, render_plan_digest

from .coordinator import (
    BatchCoordinator as _BaseBatchCoordinator,
    BatchIntegrityError,
    _atomic_write_model,
    _link_attempt,
)
from .models import BatchItemSnapshot


def _clone_frozen_render_attempt(
    coordinator: _BaseBatchCoordinator,
    plan: RenderPlan,
    *,
    purpose: str,
) -> StoredJob:
    """Create a new queued PR7 attempt from an already-authenticated persisted plan.

    This intentionally does not re-run `RenderOrchestrator.submit()`: that method proves
    a *new* plan against current Project metadata. Recovery needs a different invariant —
    execute the exact plan that was accepted into the batch before later Project edits.
    Source bytes are still hash-verified by PR7 immediately before FFmpeg execution.
    """

    profile_purpose = plan.output_profile.properties.get("purpose")
    if profile_purpose != purpose:
        raise BatchIntegrityError(
            "frozen retry purpose differs from persisted output-profile purpose"
        )
    job = StoredJob(
        project_id=plan.project_id,
        job_type="render",
        state="queued",
        payload={},
    )
    directory_key = f"renders/{plan.project_id}/{job.job_id}"
    plan_key = f"{directory_key}/plan.json"
    command_key = f"{directory_key}/command-manifest.json"
    output_key = f"{directory_key}/artifact.{plan.output_profile.container}"
    manifest_key = f"{directory_key}/artifact-manifest.json"
    failure_key = f"{directory_key}/failure-manifest.json"
    job = job.validated_copy(
        update={
            "payload": {
                "purpose": purpose,
                "profile_id": plan.output_profile.profile_id,
                "variant_id": plan.variant_id,
                "template_id": plan.template_id,
                "template_version": plan.template_version,
                "render_plan_digest": render_plan_digest(plan),
                "plan_storage_key": plan_key,
                "command_manifest_storage_key": command_key,
                "output_storage_key": output_key,
                "manifest_storage_key": manifest_key,
                "failure_storage_key": failure_key,
                "recovered_from_frozen_batch_plan": True,
            }
        }
    )
    directory = coordinator.library.paths.root / directory_key
    created = False
    try:
        directory.mkdir(parents=True, exist_ok=False)
        created = True
        _atomic_write_model(coordinator.library.paths.root / plan_key, plan)
        coordinator.library.database.create_job(job)
    except BaseException:
        if created:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return job


class BatchCoordinator(_BaseBatchCoordinator):
    """Batch coordinator whose interrupted retries never re-read mutable Project intent."""

    def _new_retry(
        self,
        batch_job_id: str,
        item: BatchItemSnapshot,
        previous: StoredJob,
        attempt_index: int,
    ) -> StoredJob:
        plan = self.render.load_plan(previous.job_id)
        if render_plan_digest(plan) != item.render_plan_digest:
            raise BatchIntegrityError("retry plan differs from frozen batch item")
        child = _clone_frozen_render_attempt(self, plan, purpose=item.purpose)
        return _link_attempt(
            self.library,
            child,
            batch_job_id=batch_job_id,
            item_key=item.item_key,
            attempt_index=attempt_index,
        )


__all__ = ["BatchCoordinator"]
