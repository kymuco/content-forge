"""PR17 crash-safe batch submission and frozen-plan retry hardening."""

from __future__ import annotations

from collections.abc import Sequence

from content_forge.storage import StoredJob, transition_job_state
from content_forge.timeline import RenderPlan, render_plan_digest

from .coordinator import (
    BatchCoordinator as _BaseBatchCoordinator,
    BatchIntegrityError,
    BatchPreparationError,
    BatchRenderInput,
    _BATCH_CONTRACT_VERSION,
    _BatchPaths,
    _accepted_state,
    _atomic_write_model,
    _source_fingerprints,
    _validate_localized_snapshot,
)
from .models import BatchItemSnapshot, BatchManifest, canonical_digest


def _planned_asset_metadata(planned: object) -> tuple[object, ...]:
    return (
        planned.asset_id,  # type: ignore[attr-defined]
        planned.sha256,  # type: ignore[attr-defined]
        planned.media_type,  # type: ignore[attr-defined]
        planned.mime_type,  # type: ignore[attr-defined]
        planned.storage_key,  # type: ignore[attr-defined]
        planned.width,  # type: ignore[attr-defined]
        planned.height,  # type: ignore[attr-defined]
        planned.duration_seconds,  # type: ignore[attr-defined]
        planned.has_audio,  # type: ignore[attr-defined]
    )


def _stored_asset_metadata(stored: object) -> tuple[object, ...]:
    return (
        stored.asset_id,  # type: ignore[attr-defined]
        stored.sha256,  # type: ignore[attr-defined]
        stored.media_type,  # type: ignore[attr-defined]
        stored.mime_type,  # type: ignore[attr-defined]
        stored.storage_key,  # type: ignore[attr-defined]
        stored.width,  # type: ignore[attr-defined]
        stored.height,  # type: ignore[attr-defined]
        stored.duration_seconds,  # type: ignore[attr-defined]
        stored.has_audio,  # type: ignore[attr-defined]
    )


def _plan_source_pairs(plan: RenderPlan) -> tuple[tuple[str | None, str], ...]:
    pairs: list[tuple[str | None, str]] = []
    for scene in plan.scenes:
        if scene.media_source_id is not None:
            pairs.append((scene.media_asset_id, scene.media_source_id))
    for overlay in plan.overlays:
        if overlay.source_id is not None:
            pairs.append((overlay.asset_id, overlay.source_id))
    for track in plan.audio_tracks:
        if track.source_id is not None:
            pairs.append((track.asset_id, track.source_id))
    return tuple(pairs)


def _validate_initial_plan(
    coordinator: _BaseBatchCoordinator,
    plan: RenderPlan,
    *,
    purpose: str,
) -> None:
    """Mirror PR7 submission invariants before the batch-aware initial INSERT."""

    project = coordinator.library.load_project(plan.project_id)
    if project is None:
        raise BatchPreparationError(
            f"render plan project is not stored in local library: {plan.project_id}"
        )
    if plan.output_profile.properties.get("purpose") != purpose:
        raise BatchPreparationError(
            "render purpose must match output profile properties['purpose']"
        )
    stored_profile = next(
        (
            profile
            for profile in project.output_profiles
            if profile.profile_id == plan.output_profile.profile_id
        ),
        None,
    )
    if stored_profile is None or stored_profile != plan.output_profile:
        raise BatchPreparationError(
            "render-plan output profile differs from the stored project"
        )

    if project.variants:
        if plan.variant_id is None or plan.variant_language is None:
            raise BatchPreparationError(
                "variant project render plan must preserve variant identity"
            )
        stored_variant = next(
            (item for item in project.variants if item.variant_id == plan.variant_id),
            None,
        )
        if stored_variant is None or stored_variant.language != plan.variant_language:
            raise BatchPreparationError(
                "render-plan variant ID/language differs from the stored project"
            )
    elif plan.variant_id is not None or plan.variant_language is not None:
        raise BatchPreparationError(
            "render plan carries variant identity for a project without variants"
        )

    project_template = (
        (None, None)
        if project.template is None
        else (project.template.template_id, project.template.version)
    )
    if (plan.template_id, plan.template_version) != project_template:
        raise BatchPreparationError(
            "render-plan template identity differs from the stored project"
        )

    for planned in plan.assets:
        stored = coordinator.library.database.get_asset(planned.asset_id)
        if stored is None:
            raise BatchPreparationError(
                f"render-plan asset is not stored in local library: {planned.asset_id}"
            )
        if _planned_asset_metadata(planned) != _stored_asset_metadata(stored):
            raise BatchPreparationError(
                f"render-plan asset metadata differs from library metadata: {planned.asset_id}"
            )

    project_sources = {
        record.source_id: record.asset_id for record in project.source_records
    }
    for asset_id, source_id in _plan_source_pairs(plan):
        if asset_id is None or project_sources.get(source_id) != asset_id:
            raise BatchPreparationError(
                "render-plan provenance differs from stored project provenance"
            )
        source = coordinator.library.database.get_source(source_id)
        if source is None or source.asset_id != asset_id:
            raise BatchPreparationError(
                "render-plan provenance differs from authoritative library source"
            )


def _create_batch_render_attempt(
    coordinator: _BaseBatchCoordinator,
    plan: RenderPlan,
    *,
    purpose: str,
    batch_job_id: str,
    item_key: str,
    attempt_index: int,
    validate_current_project: bool,
    recovered: bool,
) -> StoredJob:
    """Create plan file + queued job with batch identity present in the first DB row."""

    if validate_current_project:
        _validate_initial_plan(coordinator, plan, purpose=purpose)
    elif plan.output_profile.properties.get("purpose") != purpose:
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
    payload: dict[str, object] = {
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
        "batch_context": {
            "batch_job_id": batch_job_id,
            "item_key": item_key,
            "attempt_index": attempt_index,
        },
    }
    if recovered:
        payload["recovered_from_frozen_batch_plan"] = True
    job = job.validated_copy(update={"payload": payload})

    directory = coordinator.library.paths.root / directory_key
    created = False
    try:
        directory.mkdir(parents=True, exist_ok=False)
        created = True
        _atomic_write_model(coordinator.library.paths.root / plan_key, plan)
        coordinator.library.database.create_job(job)
    except BaseException:
        # A crash before the DB INSERT can leave only an unreferenced plan directory;
        # no generic worker can see or execute it. Ordinary exceptions clean it now.
        if created:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)
        raise
    return job


class BatchCoordinator(_BaseBatchCoordinator):
    """Coordinator with atomic child linkage and frozen-plan-only interrupted retries."""

    def prepare(self, inputs: Sequence[BatchRenderInput]) -> StoredJob:
        if not inputs:
            raise BatchPreparationError("batch requires at least one render item")
        if len(inputs) > 10000:
            raise BatchPreparationError("batch exceeds supported item count")

        parent = StoredJob(
            project_id=None,
            job_type="batch",
            state="preparing",
            payload={"batch_contract_version": _BATCH_CONTRACT_VERSION},
        )
        self.library.database.create_job(parent)
        paths = _BatchPaths.for_batch(parent.job_id)
        directory = self.library.paths.root / paths.directory_key
        created_children: list[str] = []
        try:
            directory.mkdir(parents=True, exist_ok=False)
            items: list[BatchItemSnapshot] = []
            for index, value in enumerate(inputs):
                plan = value.plan
                item_key = f"item_{index:04d}"
                project = self.library.load_project(plan.project_id)
                if project is None:
                    raise BatchPreparationError(
                        f"batch project is not stored in local library: {plan.project_id}"
                    )
                localized = _validate_localized_snapshot(
                    project,
                    plan,
                    value.localized_variant,
                )
                child = _create_batch_render_attempt(
                    self,
                    plan,
                    purpose=value.purpose,
                    batch_job_id=parent.job_id,
                    item_key=item_key,
                    attempt_index=0,
                    validate_current_project=True,
                    recovered=False,
                )
                created_children.append(child.job_id)
                items.append(
                    BatchItemSnapshot(
                        item_key=item_key,
                        project_id=plan.project_id,
                        purpose=value.purpose,
                        profile_id=plan.output_profile.profile_id,
                        variant_id=plan.variant_id,
                        variant_language=plan.variant_language,
                        template_id=plan.template_id,
                        template_version=plan.template_version,
                        render_plan_digest=render_plan_digest(plan),
                        source_assets=_source_fingerprints(plan),
                        localized_variant=localized,
                        accepted_state=_accepted_state(project, plan, localized),
                        initial_job_id=child.job_id,
                    )
                )
            manifest = BatchManifest(batch_job_id=parent.job_id, items=tuple(items))
            _atomic_write_model(self.library.paths.root / paths.manifest_key, manifest)
            return transition_job_state(
                self.library.database,
                parent.job_id,
                expected_state="preparing",
                state="queued",
                payload_additions={
                    "batch_manifest_storage_key": paths.manifest_key,
                    "batch_manifest_digest": canonical_digest(manifest),
                    "batch_item_count": len(items),
                },
            )
        except BaseException as exc:
            for child_id in created_children:
                child = self.library.database.get_job(child_id)
                if child is None or child.state != "queued":
                    continue
                try:
                    transition_job_state(
                        self.library.database,
                        child.job_id,
                        expected_state="queued",
                        state="cancelled",
                        payload_additions={"batch_preparation_aborted": True},
                    )
                except Exception:
                    pass
            try:
                transition_job_state(
                    self.library.database,
                    parent.job_id,
                    expected_state="preparing",
                    state="failed",
                    payload_additions={
                        "preparation_error": (
                            str(exc).strip() or type(exc).__name__
                        )[:4096]
                    },
                )
            except Exception:
                pass
            raise

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
        return _create_batch_render_attempt(
            self,
            plan,
            purpose=item.purpose,
            batch_job_id=batch_job_id,
            item_key=item.item_key,
            attempt_index=attempt_index,
            validate_current_project=False,
            recovered=True,
        )


__all__ = ["BatchCoordinator"]
