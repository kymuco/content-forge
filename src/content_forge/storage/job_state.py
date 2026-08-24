"""Atomic compare-and-set transitions for persistent job metadata."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .database import LibraryDatabase, StorageConflictError, StorageError
from .records import StoredJob


def transition_job_state(
    database: LibraryDatabase,
    job_id: str,
    *,
    expected_state: str,
    state: str,
) -> StoredJob:
    """Atomically replace one job state only when the expected state still matches.

    `LibraryDatabase.transaction()` uses `BEGIN IMMEDIATE`, so the read/compare/write
    sequence is serialized with competing writers. The SQL predicate is retained as a
    second fail-closed guard and makes the intended compare-and-set contract explicit.
    """

    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"unknown job: {job_id}")

        current = StoredJob(
            job_id=row["job_id"],
            project_id=row["project_id"],
            job_type=row["job_type"],
            state=row["state"],
            payload=json.loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        if current.state != expected_state:
            raise StorageConflictError(
                f"job {job_id} state changed: expected {expected_state!r}, "
                f"found {current.state!r}"
            )

        updated = current.validated_copy(
            update={"state": state, "updated_at": datetime.now(timezone.utc)}
        )
        changed = connection.execute(
            """
            UPDATE jobs
            SET state = ?, updated_at = ?
            WHERE job_id = ? AND state = ?
            """,
            (
                updated.state,
                updated.updated_at.isoformat(),
                job_id,
                expected_state,
            ),
        ).rowcount
        if changed != 1:
            raise StorageConflictError(
                f"job {job_id} could not transition from {expected_state!r} to {state!r}"
            )
        return updated
