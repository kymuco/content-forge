"""Final PR17 coordinator hardening for live ownership and linear attempt lookup."""

from __future__ import annotations

from collections import defaultdict

from content_forge.core import EntityKind, require_entity_id
from content_forge.storage import StoredJob, list_jobs

from .coordinator import (
    BatchIntegrityError,
    BatchRunError,
    _batch_context,
    _context_int,
    _failure_code,
    _interrupt_running_attempt,
)
from .hardened import (
    BatchCoordinator as _HardenedBatchCoordinator,
    _interrupt_stale_queued_claim,
)
from .lease import BatchLeaseBusyError, BatchRunLease
from .models import BatchItemSnapshot


class BatchCoordinator(_HardenedBatchCoordinator):
    """Public PR17 coordinator with one live owner and O(jobs + batch items) indexing."""

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
            from content_forge.storage import transition_job_state

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

    def run_batch(self, batch_job_id, capabilities, **kwargs):
        """Drain one batch under an OS lease; a live runner can never look stale."""

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
                raise BatchRunError("batch attempt index is already active in this coordinator")
            caches[batch_job_id] = self._index_batch_attempts(batch_job_id)
            try:
                return super().run_batch(
                    batch_job_id,
                    capabilities,
                    **kwargs,
                )
            finally:
                caches.pop(batch_job_id, None)


__all__ = ["BatchCoordinator"]
