from __future__ import annotations

from pathlib import Path

import pytest

import content_forge.batch.final as batch_final
from content_forge.batch import (
    AcceptedStateSnapshot,
    BatchCoordinator,
    BatchItemSnapshot,
    BatchRunError,
)
from content_forge.batch.lease import BatchRunLease
from content_forge.storage import LocalLibrary, StoredJob


def _batch_id(ch: str) -> str:
    return "cf_job_" + ch * 32


def test_live_batch_lease_rejects_concurrent_runner_before_durable_state_changes(
    tmp_path: Path,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    coordinator = BatchCoordinator(library)
    batch_job_id = _batch_id("a")
    parent = StoredJob(
        job_id=batch_job_id,
        project_id=None,
        job_type="batch",
        state="running",
        payload={"sentinel": "unchanged"},
    )
    library.database.create_job(parent)
    lock_path = library.paths.root / "batches" / batch_job_id / ".run.lock"

    with BatchRunLease.acquire(lock_path):
        with pytest.raises(BatchRunError, match="already owned by a live runner"):
            coordinator.run_batch(batch_job_id, object())  # type: ignore[arg-type]

    persisted = library.database.get_job(batch_job_id)
    assert persisted is not None
    assert persisted.state == "running"
    assert persisted.payload == {"sentinel": "unchanged"}


def test_run_batch_indexes_render_attempts_once_for_all_item_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    coordinator = BatchCoordinator(library)
    batch_job_id = _batch_id("b")

    items: list[BatchItemSnapshot] = []
    for index in range(5):
        item_key = f"item_{index:04d}"
        job = StoredJob(
            project_id=None,
            job_type="render",
            state="batch_held",
            payload={
                "batch_context": {
                    "batch_job_id": batch_job_id,
                    "item_key": item_key,
                    "attempt_index": 0,
                }
            },
        )
        library.database.create_job(job)
        items.append(
            BatchItemSnapshot(
                item_key=item_key,
                project_id="cf_project_" + "1" * 32,
                purpose="preview",
                profile_id="test_preview",
                render_plan_digest="0" * 64,
                accepted_state=AcceptedStateSnapshot(),
                initial_job_id=job.job_id,
            )
        )

    expected_item_keys = {item.item_key for item in items}

    # Historical/unrelated renders may exist; the public drain still performs one scan,
    # then every per-item `_current_attempt` lookup uses the in-memory batch index.
    library.database.create_job(
        StoredJob(
            project_id=None,
            job_type="render",
            state="succeeded",
            payload={"historical": True},
        )
    )

    original_list_jobs = batch_final.list_jobs
    scan_count = 0

    def counted_list_jobs(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        return original_list_jobs(*args, **kwargs)

    def fake_hardened_run(self, requested_batch_job_id, capabilities, **kwargs):
        del capabilities, kwargs
        resolved = []
        for item in items:
            attempt, attempt_index = self._current_attempt(  # noqa: SLF001
                requested_batch_job_id,
                item,
                "single-public-drain",
            )
            assert attempt_index == 0
            assert attempt.state == "queued"
            resolved.append(attempt.job_id)
        return tuple(resolved)

    monkeypatch.setattr(batch_final, "list_jobs", counted_list_jobs)
    monkeypatch.setattr(
        batch_final._HardenedBatchCoordinator,  # noqa: SLF001
        "run_batch",
        fake_hardened_run,
    )

    result = coordinator.run_batch(batch_job_id, object())  # type: ignore[arg-type]

    assert len(result) == len(expected_item_keys)
    assert scan_count == 1
    assert batch_job_id not in coordinator._pr17_attempt_index  # noqa: SLF001
