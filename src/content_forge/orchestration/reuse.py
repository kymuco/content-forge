"""Authenticated reuse lookup for already-succeeded immutable render attempts."""

from __future__ import annotations

from content_forge.storage import LocalLibrary, list_jobs
from content_forge.timeline import RenderPlan, render_plan_digest

from .models import RenderArtifactManifest, RenderPurpose
from .render_jobs import RenderJobIntegrityError, RenderOrchestrator


class RenderReuseIntegrityError(RenderJobIntegrityError):
    """A matching cache candidate exists but its authenticated evidence is invalid."""


def _payload_matches(
    payload: object,
    *,
    purpose: RenderPurpose,
    plan: RenderPlan,
    digest: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("purpose") == purpose
        and payload.get("profile_id") == plan.output_profile.profile_id
        and payload.get("variant_id") == plan.variant_id
        and payload.get("template_id") == plan.template_id
        and payload.get("template_version") == plan.template_version
        and payload.get("render_plan_digest") == digest
    )


def find_reusable_render_artifact(
    library: LocalLibrary,
    plan: RenderPlan,
    *,
    purpose: RenderPurpose,
    ffprobe_path: str = "ffprobe",
    probe_timeout: float = 20.0,
) -> RenderArtifactManifest | None:
    """Return the newest exact authenticated artifact for ``plan``, or ``None``.

    This creates no second cache authority. Candidate identity lives in the existing
    render-job SQLite receipt, and acceptance goes through ``RenderOrchestrator``'s
    persisted-plan and artifact verification before any prior output may be reused.
    """

    if plan.output_profile.properties.get("purpose") != purpose:
        raise RenderReuseIntegrityError(
            "render reuse purpose must match output profile purpose"
        )

    digest = render_plan_digest(plan)
    orchestrator = RenderOrchestrator(library)
    candidates = tuple(
        job
        for job in list_jobs(library.database, job_type="render", state="succeeded")
        if job.project_id == plan.project_id
        and _payload_matches(
            dict(job.payload),
            purpose=purpose,
            plan=plan,
            digest=digest,
        )
    )
    for job in reversed(candidates):
        try:
            persisted_plan = orchestrator.load_plan(job.job_id)
            artifact = orchestrator.load_artifact(
                job.job_id,
                ffprobe_path=ffprobe_path,
                probe_timeout=probe_timeout,
            )
        except RenderJobIntegrityError as exc:
            raise RenderReuseIntegrityError(
                f"matching render reuse candidate failed integrity verification: {job.job_id}"
            ) from exc
        if persisted_plan != plan:
            raise RenderReuseIntegrityError(
                "matching render reuse digest does not preserve the exact RenderPlan"
            )
        if artifact is None:
            raise RenderReuseIntegrityError(
                "succeeded render reuse candidate has no authenticated artifact"
            )
        if (
            artifact.render_plan_digest != digest
            or artifact.project_id != plan.project_id
            or artifact.purpose != purpose
            or artifact.profile_id != plan.output_profile.profile_id
        ):
            raise RenderReuseIntegrityError(
                "authenticated render reuse artifact identity does not match requested plan"
            )
        return artifact
    return None


__all__ = [
    "RenderReuseIntegrityError",
    "find_reusable_render_artifact",
]
