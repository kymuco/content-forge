from __future__ import annotations

from pathlib import Path

import pytest

import content_forge.batch.final as batch_final
from content_forge.batch import BatchCoordinator, BatchRunError
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
    lock_path = library.paths.root / "batches" / batch_job_id / ".run.lock"

    with BatchRunLease.acquire(lock_path):
        with pytest.raises(BatchRunError, match="already owned by a live runner"):
            coordinator.run_batch(batch_job_id, object())  # type: ignore[arg-type]

    # The lease check happens before the coordinator asks SQLite for the batch job.
    assert library.database.get_job(batch_job_id) is None


def test_run_batch_indexes_render_attempts_once_for_all_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    coordinator = BatchCoordinator(library)
    batch_job_id = _batch_id("b")

    expected_item_keys = {f"item_{index:04d}" for index in range(5)}
    for index, item_key in enumerate(sorted(expected_item_keys)):
        library.database.create_job(
            StoredJob(
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
        )

    # Historical/unrelated renders may exist; the public drain still performs one scan,
    # then all per-item lookups use the in-memory batch index.
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
        indexed = self._pr17_attempt_index[requested_batch_job_id]  # noqa: SLF001
        assert set(indexed) == expected_item_keys
        assert all(len(attempts) == 1 for attempts in indexed.values())
        return tuple(attempts[0].job_id for attempts in indexed.values())

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
