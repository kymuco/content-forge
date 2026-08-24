"""Atomic compare-and-set transitions for persistent job metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone

from .database import LibraryDatabase, StorageConflictError, StorageError
from .records import StoredJob


def _strict_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def transition_job_state(
    database: LibraryDatabase,
    job_id: str,
    *,
    expected_state: str,
    state: str,
    payload_additions: Mapping[str, object] | None = None,
) -> StoredJob:
    """Atomically replace one job state and append trusted receipt fields.

    `LibraryDatabase.transaction()` uses `BEGIN IMMEDIATE`, so the read/compare/write
    sequence is serialized with competing writers. The SQL predicate retains the
    current state and exact payload as a second fail-closed compare-and-set guard.

    `payload_additions` is append-only: an existing payload key may never be replaced.
    This keeps submission fields immutable while allowing terminal transitions to bind
    digests or other one-time execution receipts in authoritative SQLite state.
    """

    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"unknown job: {job_id}")

        current_payload_json = row["payload_json"]
        current = StoredJob(
            job_id=row["job_id"],
            project_id=row["project_id"],
            job_type=row["job_type"],
            state=row["state"],
            payload=json.loads(current_payload_json),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        if current.state != expected_state:
            raise StorageConflictError(
                f"job {job_id} state changed: expected {expected_state!r}, "
                f"found {current.state!r}"
            )

        # FrozenModel recursively freezes JSON containers. Re-serialize through JSON
        # mode before applying receipt fields so nested payload objects are ordinary
        # JSON-compatible dict/list values for full Pydantic revalidation.
        payload_value = current.model_dump(mode="json")["payload"]
        if not isinstance(payload_value, dict):
            raise StorageConflictError("job payload did not serialize to an object")
        payload = payload_value
        if payload_additions:
            for key, value in payload_additions.items():
                if not isinstance(key, str) or not key:
                    raise StorageConflictError("job payload receipt key must be non-empty")
                if key in payload:
                    raise StorageConflictError(
                        f"job payload receipt field already exists: {key}"
                    )
                payload[key] = value

        updated = current.validated_copy(
            update={
                "state": state,
                "payload": payload,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        updated_payload_json = _strict_json(
            updated.model_dump(mode="json")["payload"]
        )
        changed = connection.execute(
            """
            UPDATE jobs
            SET state = ?, payload_json = ?, updated_at = ?
            WHERE job_id = ? AND state = ? AND payload_json = ?
            """,
            (
                updated.state,
                updated_payload_json,
                updated.updated_at.isoformat(),
                job_id,
                expected_state,
                current_payload_json,
            ),
        ).rowcount
        if changed != 1:
            raise StorageConflictError(
                f"job {job_id} could not transition from {expected_state!r} to {state!r}"
            )
        return updated
