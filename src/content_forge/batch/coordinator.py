"""PR17 durable batch coordinator built on immutable PR7 render attempts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from content_forge.core import Project, ReviewStatus
from content_forge.orchestration import (
    RenderFailureManifest,
    RenderOrchestrator,
    RenderPurpose,
    RenderSourceFingerprint,
)
from content_forge.render.ffmpeg import (
    FFmpegCapabilities,
    RenderCommandManifest,
    command_manifest_digest,
)
from content_forge.render.ffmpeg.models import FFMPEG_BACKEND_VERSION
from content_forge.storage import (
    LocalLibrary,
    StorageConflictError,
    StoredJob,
    list_jobs,
    transition_job_state,
)
from content_forge.timeline import RenderPlan, render_plan_digest
from content_forge.variants import (
    CompiledLanguageVariant,
    LocalizedVariantSnapshot,
    localized_variant_digest,
    localized_variant_snapshot,
)

from .models import (
    AcceptedStateSnapshot,
    BatchItemResult,
    BatchItemSnapshot,
    BatchManifest,
    BatchResultManifest,
    ExportSidecar,
    ProviderParameterSnapshot,
    RenderQCReport,
    canonical_digest,
)
from .qc import run_render_qc

_BATCH_CONTRACT_VERSION = "1"


class BatchError(RuntimeError):
    """Base class for PR17 batch preparation/execution failures."""


class BatchPreparationError(BatchError):
    pass


class BatchIntegrityError(BatchError):
    pass


class BatchRunError(BatchError):
    pass


@dataclass(frozen=True, slots=True)
class BatchRenderInput:
    """In-memory preparation input; persisted authority is BatchManifest + child plans."""

    plan: RenderPlan
    purpose: RenderPurpose
    localized_variant: LocalizedVariantSnapshot | None = None

    @classmethod
    def from_compiled_language_variant(
        cls,
        compiled: CompiledLanguageVariant,
        *,
        purpose: RenderPurpose,
    ) -> "BatchRenderInput":
        return cls(
            plan=compiled.plan,
            purpose=purpose,
            localized_variant=compiled.localized_variant,
        )


@dataclass(frozen=True, slots=True)
class _BatchPaths:
    directory_key: str
    manifest_key: str
    result_key: str

    @classmethod
    def for_batch(cls, batch_job_id: str) -> "_BatchPaths":
        directory = f"batches/{batch_job_id}"
        return cls(
            directory_key=directory,
            manifest_key=f"{directory}/batch-manifest.json",
            result_key=f"{directory}/batch-result.json",
        )

    def qc_key(self, item_key: str) -> str:
        return f"{self.directory_key}/items/{item_key}/qc-report.json"

    def export_key(self, item_key: str) -> str:
        return f"{self.directory_key}/items/{item_key}/export-sidecar.json"


def _strict_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def _atomic_write_model(path: Path, model: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_strict_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_descriptor = None
            if directory_descriptor is not None:
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BatchIntegrityError(f"batch payload has invalid {key}")
    return value


def _accepted_state(
    project: Project,
    plan: RenderPlan,
    localized: LocalizedVariantSnapshot | None,
) -> AcceptedStateSnapshot:
    rendered_text = {
        overlay.overlay_id: overlay.text
        for overlay in plan.overlays
        if overlay.text is not None
    }
    localized_metadata = (
        {} if localized is None else localized.model_dump(mode="json")
    )
    review_acceptances: dict[str, object] = {}
    provider_parameters: list[ProviderParameterSnapshot] = []
    for task in project.review_tasks:
        if task.status is not ReviewStatus.RESOLVED or task.accepted_value is None:
            continue
        task_payload = task.model_dump(mode="json")
        accepted_value = task_payload["accepted_value"]
        review_acceptances[task.review_task_id] = accepted_value
        accepted_digest = canonical_digest(accepted_value)
        for suggestion, suggestion_payload in zip(task.suggestions, task_payload["suggestions"]):
            if suggestion.provider is None:
                continue
            if canonical_digest(suggestion_payload["value"]) != accepted_digest:
                continue
            provider_parameters.append(
                ProviderParameterSnapshot(
                    provider=suggestion.provider,
                    task_type=task.task_type,
                    review_task_id=task.review_task_id,
                    suggestion_id=suggestion.suggestion_id,
                    metadata=suggestion_payload["metadata"],
                )
            )
    return AcceptedStateSnapshot(
        rendered_text=rendered_text,
        localized_metadata=localized_metadata,
        review_acceptances=review_acceptances,
        provider_parameters=tuple(provider_parameters),
    )


def _source_fingerprints(plan: RenderPlan) -> tuple[RenderSourceFingerprint, ...]:
    return tuple(
        RenderSourceFingerprint(
            asset_id=item.asset_id,
            sha256=item.sha256,
            storage_key=item.storage_key,
        )
        for item in sorted(plan.assets, key=lambda value: value.asset_id)
    )


def _validate_localized_snapshot(
    project: Project,
    plan: RenderPlan,
    localized: LocalizedVariantSnapshot | None,
) -> LocalizedVariantSnapshot | None:
    if plan.variant_id is None:
        if localized is not None:
            raise BatchPreparationError(
                "non-variant render plan cannot carry a localized variant snapshot"
            )
        return None
    if localized is None:
        raise BatchPreparationError(
            "variant render plan requires the exact PR16 localized variant snapshot"
        )
    if (localized.variant_id, localized.language) != (
        plan.variant_id,
        plan.variant_language,
    ):
        raise BatchPreparationError(
            "localized variant snapshot does not match render-plan variant identity"
        )
    variant = next(
        (item for item in project.variants if item.variant_id == plan.variant_id),
        None,
    )
    if variant is None:
        raise BatchPreparationError("render-plan variant is missing from stored project")
    current = localized_variant_snapshot(variant)
    if current != localized:
        raise BatchPreparationError(
            "stored project localized metadata changed before batch preparation"
        )
    return localized


def _batch_context(job: StoredJob) -> Mapping[str, object] | None:
    value = job.payload.get("batch_context")
    return value if isinstance(value, Mapping) else None


def _context_int(context: Mapping[str, object], key: str) -> int:
    value = context.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BatchIntegrityError(f"render batch context has invalid {key}")
    return value


def _linked_attempts(
    library: LocalLibrary,
    *,
    batch_job_id: str,
    item_key: str,
) -> tuple[StoredJob, ...]:
    matches: list[tuple[int, StoredJob]] = []
    for job in list_jobs(library.database, job_type="render"):
        context = _batch_context(job)
        if context is None:
            continue
        if context.get("batch_job_id") != batch_job_id or context.get("item_key") != item_key:
            continue
        matches.append((_context_int(context, "attempt_index"), job))
    matches.sort(key=lambda pair: (pair[0], pair[1].created_at, pair[1].job_id))
    indices = [index for index, _ in matches]
    if len(indices) != len(set(indices)):
        raise BatchIntegrityError(f"duplicate attempt index for batch item {item_key}")
    if indices and indices != list(range(indices[-1] + 1)):
        raise BatchIntegrityError(f"non-contiguous attempt history for batch item {item_key}")
    return tuple(job for _, job in matches)


def _link_attempt(
    library: LocalLibrary,
    job: StoredJob,
    *,
    batch_job_id: str,
    item_key: str,
    attempt_index: int,
) -> StoredJob:
    try:
        return transition_job_state(
            library.database,
            job.job_id,
            expected_state="queued",
            state="queued",
            payload_additions={
                "batch_context": {
                    "batch_job_id": batch_job_id,
                    "item_key": item_key,
                    "attempt_index": attempt_index,
                }
            },
        )
    except StorageConflictError as exc:
        raise BatchPreparationError("render attempt could not be linked to batch") from exc


def _mark_attempt_run_instance(
    library: LocalLibrary,
    job: StoredJob,
    run_instance_id: str,
) -> StoredJob:
    if "batch_run_instance_id" in job.payload:
        existing = job.payload["batch_run_instance_id"]
        if existing != run_instance_id:
            raise BatchIntegrityError("queued render attempt already belongs to another batch run")
        return job
    try:
        return transition_job_state(
            library.database,
            job.job_id,
            expected_state="queued",
            state="queued",
            payload_additions={"batch_run_instance_id": run_instance_id},
        )
    except StorageConflictError as exc:
        raise BatchRunError("queued render attempt changed before execution") from exc


def _interrupt_running_attempt(
    library: LocalLibrary,
    orchestrator: RenderOrchestrator,
    job: StoredJob,
    *,
    current_run_instance_id: str,
) -> StoredJob:
    if job.state != "running":
        return job
    old_run_instance = job.payload.get("batch_run_instance_id")
    if not isinstance(old_run_instance, str) or not old_run_instance:
        raise BatchIntegrityError(
            "running batch render attempt has no persisted run-instance identity"
        )
    if old_run_instance == current_run_instance_id:
        raise BatchRunError("batch render attempt is already active in this run instance")

    failure_key = _payload_string(job.payload, "failure_storage_key")
    output_key = _payload_string(job.payload, "output_storage_key")
    manifest_key = _payload_string(job.payload, "manifest_storage_key")
    command_key = _payload_string(job.payload, "command_manifest_storage_key")
    for key in (output_key, manifest_key):
        try:
            (library.paths.root / key).unlink(missing_ok=True)
        except OSError:
            pass

    failure = RenderFailureManifest(
        job_id=job.job_id,
        project_id=job.project_id or "",
        purpose=_payload_string(job.payload, "purpose"),
        profile_id=_payload_string(job.payload, "profile_id"),
        render_plan_digest=_payload_string(job.payload, "render_plan_digest"),
        failure_storage_key=failure_key,
        state="failed",
        code="render_interrupted",
        stage="batch_recovery",
        message="render attempt belonged to a previous process run and was interrupted",
        exception_type="content_forge.batch.BatchInterruptedRender",
        details={"previous_run_instance_id": old_run_instance},
    )
    _atomic_write_model(library.paths.root / failure_key, failure)
    additions: dict[str, object] = {
        "failure_manifest_digest": canonical_digest(failure),
        "batch_interrupted": True,
    }
    command_path = library.paths.root / command_key
    if command_path.is_file() and "command_manifest_digest" not in job.payload:
        try:
            command = RenderCommandManifest.model_validate_json(
                command_path.read_text(encoding="utf-8")
            )
            additions["command_manifest_digest"] = command_manifest_digest(command)
        except (OSError, ValueError):
            pass
    try:
        return transition_job_state(
            library.database,
            job.job_id,
            expected_state="running",
            state="failed",
            payload_additions=additions,
        )
    except StorageConflictError as exc:
        raise BatchRunError("interrupted render attempt changed during recovery") from exc


def _failure_code(orchestrator: RenderOrchestrator, job: StoredJob) -> tuple[str, str]:
    try:
        failure = orchestrator.load_failure(job.job_id)
    except Exception as exc:
        return "render_failed", str(exc)[:4096]
    if failure is None:
        return "render_failed", f"render attempt ended in state {job.state} without failure evidence"
    return failure.code, failure.message


class BatchCoordinator:
    """Prepare and synchronously drain durable batch jobs using PR7 render attempts."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.render = RenderOrchestrator(library)

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
                project = self.library.load_project(plan.project_id)
                if project is None:
                    raise BatchPreparationError(
                        f"batch project is not stored in local library: {plan.project_id}"
                    )
                localized = _validate_localized_snapshot(project, plan, value.localized_variant)
                child = self.render.submit(plan, purpose=value.purpose)
                created_children.append(child.job_id)
                item_key = f"item_{index:04d}"
                child = _link_attempt(
                    self.library,
                    child,
                    batch_job_id=parent.job_id,
                    item_key=item_key,
                    attempt_index=0,
                )
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
                        "preparation_error": (str(exc).strip() or type(exc).__name__)[:4096]
                    },
                )
            except Exception:
                pass
            raise

    def _batch_job(self, batch_job_id: str) -> StoredJob:
        job = self.library.database.get_job(batch_job_id)
        if job is None:
            raise BatchIntegrityError(f"unknown batch job: {batch_job_id}")
        if job.job_type != "batch" or job.project_id is not None:
            raise BatchIntegrityError("job is not a canonical cross-project batch job")
        return job

    def load_manifest(self, batch_job_id: str) -> BatchManifest:
        job = self._batch_job(batch_job_id)
        key = _payload_string(job.payload, "batch_manifest_storage_key")
        expected = _BatchPaths.for_batch(batch_job_id).manifest_key
        if key != expected:
            raise BatchIntegrityError("batch manifest storage key is not canonical")
        path = self.library.paths.root / key
        try:
            manifest = BatchManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BatchIntegrityError(f"failed to load batch manifest: {exc}") from exc
        if manifest.batch_job_id != batch_job_id:
            raise BatchIntegrityError("batch manifest identity does not match parent job")
        if canonical_digest(manifest) != _payload_string(job.payload, "batch_manifest_digest"):
            raise BatchIntegrityError("batch manifest digest changed")
        count = job.payload.get("batch_item_count")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(manifest.items):
            raise BatchIntegrityError("batch item count does not match manifest")
        return manifest

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
        child = self.render.submit(plan, purpose=item.purpose)
        return _link_attempt(
            self.library,
            child,
            batch_job_id=batch_job_id,
            item_key=item.item_key,
            attempt_index=attempt_index,
        )

    def _current_attempt(
        self,
        batch_job_id: str,
        item: BatchItemSnapshot,
        run_instance_id: str,
    ) -> tuple[StoredJob, int]:
        attempts = list(
            _linked_attempts(
                self.library,
                batch_job_id=batch_job_id,
                item_key=item.item_key,
            )
        )
        if not attempts or attempts[0].job_id != item.initial_job_id:
            raise BatchIntegrityError(
                f"batch item {item.item_key} initial render attempt is missing"
            )
        latest = attempts[-1]
        latest_index = len(attempts) - 1
        if latest.state == "running":
            latest = _interrupt_running_attempt(
                self.library,
                self.render,
                latest,
                current_run_instance_id=run_instance_id,
            )
        if latest.state == "failed":
            code, _ = _failure_code(self.render, latest)
            if code == "render_interrupted":
                latest = self._new_retry(
                    batch_job_id,
                    item,
                    latest,
                    latest_index + 1,
                )
                latest_index += 1
        return latest, latest_index

    def _write_export(
        self,
        paths: _BatchPaths,
        item: BatchItemSnapshot,
        artifact: object,
        qc: RenderQCReport,
    ) -> tuple[str, str]:
        export = ExportSidecar(
            batch_job_id=qc.batch_job_id,
            item_key=item.item_key,
            render_job_id=artifact.job_id,  # type: ignore[attr-defined]
            project_id=item.project_id,
            purpose=item.purpose,
            profile_id=item.profile_id,
            variant_id=item.variant_id,
            variant_language=item.variant_language,
            template_id=item.template_id,
            template_version=item.template_version,
            render_plan_digest=item.render_plan_digest,
            source_assets=item.source_assets,
            accepted_state=item.accepted_state,
            localized_variant_digest=(
                None
                if item.localized_variant is None
                else localized_variant_digest(item.localized_variant)
            ),
            renderer_backend_version=FFMPEG_BACKEND_VERSION,
            ffmpeg_version=artifact.ffmpeg_version,  # type: ignore[attr-defined]
            video_encoder=artifact.video_encoder,  # type: ignore[attr-defined]
            command_manifest_digest=artifact.command_manifest_digest,  # type: ignore[attr-defined]
            output_sha256=artifact.output_sha256,  # type: ignore[attr-defined]
            output_storage_key=artifact.output_storage_key,  # type: ignore[attr-defined]
            artifact_manifest_storage_key=artifact.manifest_storage_key,  # type: ignore[attr-defined]
            qc_report=qc,
        )
        key = paths.export_key(item.item_key)
        _atomic_write_model(self.library.paths.root / key, export)
        return key, canonical_digest(export)

    def run_batch(
        self,
        batch_job_id: str,
        capabilities: FFmpegCapabilities,
        *,
        prefer_nvenc: bool = True,
        render_timeout: float | None = None,
        qc_timeout: float = 60.0,
    ) -> BatchResultManifest:
        parent = self._batch_job(batch_job_id)
        if parent.state in {"succeeded", "failed"}:
            result = self.load_result(batch_job_id)
            if result is not None:
                return result
            raise BatchRunError("terminal batch job has no authenticated result manifest")
        if parent.state == "queued":
            try:
                parent = transition_job_state(
                    self.library.database,
                    parent.job_id,
                    expected_state="queued",
                    state="running",
                )
            except StorageConflictError as exc:
                raise BatchRunError("batch job changed before execution") from exc
        elif parent.state != "running":
            raise BatchRunError(f"batch job is not runnable from state {parent.state!r}")

        manifest = self.load_manifest(batch_job_id)
        paths = _BatchPaths.for_batch(batch_job_id)
        run_instance_id = uuid.uuid4().hex
        results: list[BatchItemResult] = []
        for item in manifest.items:
            try:
                attempt, attempt_index = self._current_attempt(
                    batch_job_id,
                    item,
                    run_instance_id,
                )
                if attempt.state == "queued":
                    attempt = _mark_attempt_run_instance(
                        self.library,
                        attempt,
                        run_instance_id,
                    )
                    try:
                        artifact = self.render.run_job(
                            attempt.job_id,
                            capabilities,
                            prefer_nvenc=prefer_nvenc,
                            timeout=render_timeout,
                        )
                    except BaseException:
                        current = self.library.database.get_job(attempt.job_id) or attempt
                        code, message = _failure_code(self.render, current)
                        results.append(
                            BatchItemResult(
                                item_key=item.item_key,
                                render_job_id=attempt.job_id,
                                attempt_index=attempt_index,
                                state="failed",
                                failure_code=code,
                                failure_message=message,
                            )
                        )
                        continue
                elif attempt.state == "succeeded":
                    artifact = self.render.load_artifact(
                        attempt.job_id,
                        ffprobe_path=capabilities.ffprobe_path,
                    )
                    if artifact is None:
                        raise BatchIntegrityError("successful render attempt has no artifact")
                else:
                    code, message = _failure_code(self.render, attempt)
                    results.append(
                        BatchItemResult(
                            item_key=item.item_key,
                            render_job_id=attempt.job_id,
                            attempt_index=attempt_index,
                            state="failed",
                            failure_code=code,
                            failure_message=message,
                        )
                    )
                    continue

                plan = self.render.load_plan(attempt.job_id)
                if render_plan_digest(plan) != item.render_plan_digest:
                    raise BatchIntegrityError("successful attempt plan differs from batch snapshot")
                output_path = self.library.paths.root / artifact.output_storage_key
                qc = run_render_qc(
                    batch_job_id=batch_job_id,
                    item_key=item.item_key,
                    plan=plan,
                    artifact=artifact,
                    output_path=output_path,
                    ffmpeg_path=capabilities.ffmpeg_path,
                    analysis_timeout=qc_timeout,
                )
                qc_key = paths.qc_key(item.item_key)
                _atomic_write_model(self.library.paths.root / qc_key, qc)
                export_key, export_digest = self._write_export(paths, item, artifact, qc)
                if qc.passed:
                    results.append(
                        BatchItemResult(
                            item_key=item.item_key,
                            render_job_id=attempt.job_id,
                            attempt_index=attempt_index,
                            state="succeeded",
                            qc_passed=True,
                            export_sidecar_storage_key=export_key,
                            export_sidecar_digest=export_digest,
                        )
                    )
                else:
                    results.append(
                        BatchItemResult(
                            item_key=item.item_key,
                            render_job_id=attempt.job_id,
                            attempt_index=attempt_index,
                            state="failed",
                            qc_passed=False,
                            export_sidecar_storage_key=export_key,
                            export_sidecar_digest=export_digest,
                            failure_code="qc_failed",
                            failure_message="one or more blocking QC checks failed",
                        )
                    )
            except BaseException as exc:
                attempts = _linked_attempts(
                    self.library,
                    batch_job_id=batch_job_id,
                    item_key=item.item_key,
                )
                job_id = attempts[-1].job_id if attempts else item.initial_job_id
                results.append(
                    BatchItemResult(
                        item_key=item.item_key,
                        render_job_id=job_id,
                        attempt_index=max(0, len(attempts) - 1),
                        state="failed",
                        failure_code="batch_item_failed",
                        failure_message=(str(exc).strip() or type(exc).__name__)[:4096],
                    )
                )

        status = "succeeded" if all(item.state == "succeeded" for item in results) else "failed"
        result = BatchResultManifest(
            batch_job_id=batch_job_id,
            status=status,
            items=tuple(results),
        )
        _atomic_write_model(self.library.paths.root / paths.result_key, result)
        try:
            transition_job_state(
                self.library.database,
                batch_job_id,
                expected_state="running",
                state=status,
                payload_additions={
                    "batch_result_storage_key": paths.result_key,
                    "batch_result_digest": canonical_digest(result),
                },
            )
        except StorageConflictError as exc:
            raise BatchRunError("batch terminal state changed before result publication") from exc
        return result

    def load_result(self, batch_job_id: str) -> BatchResultManifest | None:
        job = self._batch_job(batch_job_id)
        key = job.payload.get("batch_result_storage_key")
        digest = job.payload.get("batch_result_digest")
        if key is None and digest is None:
            return None
        if not isinstance(key, str) or not isinstance(digest, str):
            raise BatchIntegrityError("batch result receipt is incomplete")
        expected = _BatchPaths.for_batch(batch_job_id).result_key
        if key != expected:
            raise BatchIntegrityError("batch result storage key is not canonical")
        try:
            result = BatchResultManifest.model_validate_json(
                (self.library.paths.root / key).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise BatchIntegrityError(f"failed to load batch result: {exc}") from exc
        if result.batch_job_id != batch_job_id:
            raise BatchIntegrityError("batch result identity does not match parent job")
        if canonical_digest(result) != digest:
            raise BatchIntegrityError("batch result digest changed")
        expected_state = "succeeded" if result.status == "succeeded" else "failed"
        if job.state != expected_state:
            raise BatchIntegrityError("batch result status does not match parent job state")
        return result


__all__ = [
    "BatchCoordinator",
    "BatchError",
    "BatchIntegrityError",
    "BatchPreparationError",
    "BatchRenderInput",
    "BatchRunError",
]
