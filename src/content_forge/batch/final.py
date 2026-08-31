"""Final PR17 coordinator hardening for ownership, recovery, and linear lookup."""

from __future__ import annotations

import uuid
from collections import defaultdict

from content_forge.core import EntityKind, require_entity_id
from content_forge.storage import (
    StorageConflictError,
    StoredJob,
    list_jobs,
    transition_job_state,
)
from content_forge.timeline import render_plan_digest

from .coordinator import (
    BatchIntegrityError,
    BatchRunError,
    _BatchPaths,
    _atomic_write_model,
    _batch_context,
    _context_int,
    _failure_code,
    _interrupt_running_attempt,
    _mark_attempt_run_instance,
)
from .hardened import (
    BatchCoordinator as _HardenedBatchCoordinator,
    _interrupt_stale_queued_claim,
)
from .lease import BatchLeaseBusyError, BatchRunLease
from .models import (
    BatchItemResult,
    BatchItemSnapshot,
    BatchResultManifest,
    canonical_digest,
)
from .qc import run_render_qc


class BatchCoordinator(_HardenedBatchCoordinator):
    """Public PR17 coordinator with one live owner and O(jobs + items) lookup."""

    def _index_batch_attempts(
        self,
        batch_job_id: str,
    ) -> dict[str, list[StoredJob]]:
        grouped: dict[str, list[tuple[int, StoredJob]]] = defaultdict(list)
        for job in list_jobs(self.library.database, job_type="render"):
            context = _batch_context(job)
            if context is None or context.get("batch_job_id") != batch_job_id:
                continue
            item_key = context.get("item_key")
            if not isinstance(item_key, str) or not item_key:
                raise BatchIntegrityError("render batch context has invalid item_key")
            grouped[item_key].append((_context_int(context, "attempt_index"), job))

        indexed: dict[str, list[StoredJob]] = {}
        for item_key, pairs in grouped.items():
            pairs.sort(key=lambda pair: (pair[0], pair[1].created_at, pair[1].job_id))
            indices = [index for index, _ in pairs]
            if len(indices) != len(set(indices)):
                raise BatchIntegrityError(
                    f"duplicate attempt index for batch item {item_key}"
                )
            if indices and indices != list(range(indices[-1] + 1)):
                raise BatchIntegrityError(
                    f"non-contiguous attempt history for batch item {item_key}"
                )
            indexed[item_key] = [job for _, job in pairs]
        return indexed

    def _active_attempts(
        self,
        batch_job_id: str,
        item_key: str,
    ) -> list[StoredJob]:
        caches = getattr(self, "_pr17_attempt_index", None)
        if not isinstance(caches, dict) or batch_job_id not in caches:
            raise BatchRunError("batch attempt index is not active")
        attempts = caches[batch_job_id].get(item_key)
        return [] if attempts is None else attempts

    def _current_attempt_from_index(
        self,
        attempts: list[StoredJob],
        *,
        batch_job_id: str,
        item: BatchItemSnapshot,
        run_instance_id: str,
    ) -> tuple[StoredJob, int]:
        if not attempts or attempts[0].job_id != item.initial_job_id:
            raise BatchIntegrityError(
                f"batch item {item.item_key} initial render attempt is missing"
            )
        latest = attempts[-1]
        latest_index = len(attempts) - 1

        if latest.state == "batch_held":
            try:
                latest = transition_job_state(
                    self.library.database,
                    latest.job_id,
                    expected_state="batch_held",
                    state="queued",
                    payload_additions={"batch_released": True},
                )
            except Exception as exc:
                raise BatchRunError(
                    "held batch render attempt changed before release"
                ) from exc
            attempts[-1] = latest

        if latest.state == "queued" and "batch_run_instance_id" in latest.payload:
            latest = _interrupt_stale_queued_claim(
                self,
                latest,
                current_run_instance_id=run_instance_id,
            )
            attempts[-1] = latest
        elif latest.state == "running":
            latest = _interrupt_running_attempt(
                self.library,
                self.render,
                latest,
                current_run_instance_id=run_instance_id,
            )
            attempts[-1] = latest

        if latest.state == "failed":
            code, _ = _failure_code(self.render, latest)
            if code in {"render_interrupted", "batch_claim_interrupted"}:
                latest = self._new_retry(
                    batch_job_id,
                    item,
                    latest,
                    latest_index + 1,
                )
                attempts.append(latest)
                latest_index += 1
        return latest, latest_index

    def _current_attempt(
        self,
        batch_job_id: str,
        item: BatchItemSnapshot,
        run_instance_id: str,
    ) -> tuple[StoredJob, int]:
        caches = getattr(self, "_pr17_attempt_index", None)
        if isinstance(caches, dict) and batch_job_id in caches:
            attempts = caches[batch_job_id].setdefault(item.item_key, [])
            return self._current_attempt_from_index(
                attempts,
                batch_job_id=batch_job_id,
                item=item,
                run_instance_id=run_instance_id,
            )
        return super()._current_attempt(batch_job_id, item, run_instance_id)

    def _drain_batch(
        self,
        batch_job_id: str,
        capabilities,
        *,
        prefer_nvenc: bool,
        render_timeout: float | None,
        qc_timeout: float,
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
                attempts = self._active_attempts(batch_job_id, item.item_key)
                if attempt.state == "queued":
                    attempt = _mark_attempt_run_instance(
                        self.library,
                        attempt,
                        run_instance_id,
                    )
                    if attempts:
                        attempts[-1] = attempt
                    try:
                        artifact = self.render.run_job(
                            attempt.job_id,
                            capabilities,
                            prefer_nvenc=prefer_nvenc,
                            timeout=render_timeout,
                        )
                    except Exception:
                        current = self.library.database.get_job(attempt.job_id) or attempt
                        if attempts:
                            attempts[-1] = current
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
                    raise BatchIntegrityError(
                        "successful attempt plan differs from batch snapshot"
                    )
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
                export_key, export_digest = self._write_export(
                    paths,
                    item,
                    artifact,
                    qc,
                )
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
            except Exception as exc:
                attempts = self._active_attempts(batch_job_id, item.item_key)
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

        status = (
            "succeeded"
            if all(item.state == "succeeded" for item in results)
            else "failed"
        )
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
            raise BatchRunError(
                "batch terminal state changed before result publication"
            ) from exc
        return result

    def run_batch(
        self,
        batch_job_id,
        capabilities,
        *,
        prefer_nvenc: bool = True,
        render_timeout: float | None = None,
        qc_timeout: float = 60.0,
    ):
        """Drain one batch under an OS lease without swallowing process control."""

        try:
            batch_job_id = require_entity_id(batch_job_id, EntityKind.JOB)
        except (TypeError, ValueError) as exc:
            raise BatchIntegrityError("batch job ID is invalid") from exc

        lock_path = (
            self.library.paths.root
            / "batches"
            / batch_job_id
            / ".run.lock"
        )
        try:
            lease = BatchRunLease.acquire(lock_path)
        except BatchLeaseBusyError as exc:
            raise BatchRunError(
                "batch is already owned by a live runner"
            ) from exc

        with lease:
            caches = getattr(self, "_pr17_attempt_index", None)
            if not isinstance(caches, dict):
                caches = {}
                self._pr17_attempt_index = caches
            if batch_job_id in caches:
                raise BatchRunError(
                    "batch attempt index is already active in this coordinator"
                )
            caches[batch_job_id] = self._index_batch_attempts(batch_job_id)
            try:
                return self._drain_batch(
                    batch_job_id,
                    capabilities,
                    prefer_nvenc=prefer_nvenc,
                    render_timeout=render_timeout,
                    qc_timeout=qc_timeout,
                )
            finally:
                caches.pop(batch_job_id, None)


__all__ = ["BatchCoordinator"]
