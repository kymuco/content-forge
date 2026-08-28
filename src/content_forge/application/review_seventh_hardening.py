"""Seventh/eighth-pass PR10 hardening for final receipts and bulk preparation."""

from __future__ import annotations

import hashlib
import json

from content_forge.core import ProjectState
from content_forge.orchestration import RenderJobIntegrityError
from content_forge.timeline import render_plan_digest

from . import review as _review


_MANUAL_SETUP_FINGERPRINT = "pr10_manual_setup_input_fingerprint"


class ReviewService(_review.ReviewService):
    """Close final-receipt and complete-project preparation gaps."""

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

    def bootstrap_project(self, project_id: str):
        """Bootstrap/recheck and remember the exact non-renderable setup snapshot."""

        prepared = super().bootstrap_project(project_id)
        return self._sync_manual_setup_fingerprint(prepared.project_id)

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

        # Stored receipt fields authenticate an artifact instance, but they are not enough
        # to prove that the artifact still represents the current canonical Project. A
        # repaired/imported/generically-saved manifest may retain an old complete receipt
        # after changing render inputs, so adoption/replay also binds to today's compiled
        # final plan.
        try:
            current_plan = self._compile_plan(
                project,
                _review._base.SHORTS_FINAL_PROFILE_ID,
            )
        except _review.ReviewError:
            return None
        if render_plan_digest(current_plan) != expected_digest:
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
        """Prepare every safe Project whose relevant setup inputs require evaluation."""

        projects = self._list_projects()
        eligible = []
        for project in projects:
            if project.state not in {ProjectState.INBOX, ProjectState.NEEDS_REVIEW}:
                continue
            initialized = bool(project.metadata.get("pr10_review_initialized"))
            pending = project.metadata.get("pr10_manual_reentry_pending") is True
            renderable = bool(project.metadata.get("review_renderable"))
            if pending or not initialized:
                eligible.append(project)
                continue
            if renderable:
                continue
            current_fingerprint = self._manual_setup_input_fingerprint(project)
            if project.metadata.get(_MANUAL_SETUP_FINGERPRINT) != current_fingerprint:
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
# to this same hardened class rather than creating a bypass around these fixes.
_review.ReviewService = ReviewService


__all__ = ["ReviewService"]
