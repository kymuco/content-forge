from __future__ import annotations

import threading

import pytest

from content_forge.core import Project
from content_forge.storage import (
    LocalLibrary,
    StorageConflictError,
    StoredJob,
    transition_job_state,
)


def test_compare_and_set_job_transition_allows_only_one_claim(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project = Project(content_kind="character_moment")
    library.save_project(project)
    job = StoredJob(project_id=project.project_id, job_type="render")
    library.database.create_job(job)

    barrier = threading.Barrier(2)
    successes: list[str] = []
    conflicts: list[str] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        try:
            updated = transition_job_state(
                library.database,
                job.job_id,
                expected_state="queued",
                state="running",
            )
        except StorageConflictError as exc:
            with lock:
                conflicts.append(str(exc))
        else:
            with lock:
                successes.append(updated.state)

    threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert successes == ["running"]
    assert len(conflicts) == 1
    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "running"


def test_compare_and_set_transition_preserves_payload(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project = Project(content_kind="character_moment")
    library.save_project(project)
    job = StoredJob(
        project_id=project.project_id,
        job_type="render",
        payload={"nested": {"value": 7}},
    )
    library.database.create_job(job)

    updated = transition_job_state(
        library.database,
        job.job_id,
        expected_state="queued",
        state="running",
    )

    assert updated.payload == job.payload
    assert updated.created_at == job.created_at
    assert updated.updated_at >= job.updated_at


def test_transition_appends_trusted_receipt_without_replacing_nested_payload(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project = Project(content_kind="character_moment")
    library.save_project(project)
    job = StoredJob(
        project_id=project.project_id,
        job_type="render",
        payload={"nested": {"value": 7}, "purpose": "preview"},
    )
    library.database.create_job(job)

    updated = transition_job_state(
        library.database,
        job.job_id,
        expected_state="queued",
        state="running",
        payload_additions={"receipt_digest": "a" * 64},
    )

    assert updated.payload["nested"] == {"value": 7}
    assert updated.payload["purpose"] == "preview"
    assert updated.payload["receipt_digest"] == "a" * 64
    stored = library.database.get_job(job.job_id)
    assert stored == updated


def test_transition_receipts_are_append_only(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project = Project(content_kind="character_moment")
    library.save_project(project)
    job = StoredJob(
        project_id=project.project_id,
        job_type="render",
        payload={"purpose": "preview"},
    )
    library.database.create_job(job)

    running = transition_job_state(
        library.database,
        job.job_id,
        expected_state="queued",
        state="running",
        payload_additions={"command_manifest_digest": "a" * 64},
    )

    with pytest.raises(StorageConflictError, match="already exists"):
        transition_job_state(
            library.database,
            job.job_id,
            expected_state="running",
            state="failed",
            payload_additions={"command_manifest_digest": "b" * 64},
        )

    stored = library.database.get_job(job.job_id)
    assert stored == running
    assert stored is not None
    assert stored.state == "running"
