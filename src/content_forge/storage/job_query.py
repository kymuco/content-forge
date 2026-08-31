"""Deterministic read-only queries over persistent jobs."""

from __future__ import annotations

import json

from .database import LibraryDatabase
from .records import StoredJob


def list_jobs(
    database: LibraryDatabase,
    *,
    job_type: str | None = None,
    state: str | None = None,
) -> tuple[StoredJob, ...]:
    """Return persistent jobs in stable creation order with optional exact filters."""

    clauses: list[str] = []
    parameters: list[str] = []
    if job_type is not None:
        clauses.append("job_type = ?")
        parameters.append(job_type)
    if state is not None:
        clauses.append("state = ?")
        parameters.append(state)
    where = "" if not clauses else " WHERE " + " AND ".join(clauses)
    query = (
        "SELECT * FROM jobs"
        + where
        + " ORDER BY created_at ASC, job_id ASC"
    )
    with database.connection() as connection:
        rows = connection.execute(query, tuple(parameters)).fetchall()
    return tuple(
        StoredJob(
            job_id=row["job_id"],
            project_id=row["project_id"],
            job_type=row["job_type"],
            state=row["state"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    )


__all__ = ["list_jobs"]
