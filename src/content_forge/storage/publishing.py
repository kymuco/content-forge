"""PR27 durable publish operations and crash-safe attempt states."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from content_forge.core import EntityKind, new_entity_id, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.providers.publishing import (
    ApprovedPublishRequest,
    PublishApproval,
    PublishRequest,
    PublishResult,
    publish_idempotency_key,
    semantic_publish_request_digest,
)

from .database import LibraryDatabase, StorageConflictError, StorageSchemaError

_PUBLISHING_SCHEMA_COMPONENT = "publishing"
_PUBLISHING_SCHEMA_VERSION = 1
PublishAttemptState = Literal["prepared", "running", "succeeded", "failed", "outcome_unknown"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(model) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class PublishOperationRecord(FrozenModel):
    request_sha256: SHA256
    idempotency_key: str = Field(pattern=r"^cfp-[0-9a-f]{64}$")
    request: PublishRequest
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_identity(self):
        if self.request_sha256 != semantic_publish_request_digest(self.request):
            raise ValueError("publish operation request digest mismatch")
        if self.idempotency_key != publish_idempotency_key(self.request):
            raise ValueError("publish operation idempotency key mismatch")
        return self


class PublishAttemptRecord(FrozenModel):
    attempt_id: str
    request_sha256: SHA256
    attempt_number: int = Field(ge=1)
    state: PublishAttemptState
    approval: PublishApproval
    result: PublishResult | None = None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=8192)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def validate_state_payload(self):
        if self.approval.request_sha256 != self.request_sha256:
            raise ValueError("publish attempt approval digest mismatch")
        if self.state == "prepared":
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("prepared publish attempt cannot have execution timestamps")
        elif self.state == "running":
            if self.started_at is None or self.finished_at is not None:
                raise ValueError("running publish attempt requires only started_at")
        else:
            if self.finished_at is None:
                raise ValueError("terminal publish attempt requires finished_at")
        if self.state == "succeeded":
            if self.result is None or self.error_code is not None or self.error_message is not None:
                raise ValueError("succeeded publish attempt requires only result")
        elif self.state in {"failed", "outcome_unknown"}:
            if self.result is not None or not self.error_code or not self.error_message:
                raise ValueError("failed/unknown publish attempt requires bounded error evidence")
        elif self.result is not None or self.error_code is not None or self.error_message is not None:
            raise ValueError("non-terminal publish attempt cannot contain result/error evidence")
        return self


class PublishingRepository:
    """Additive ledger for one semantic publish operation and immutable attempts."""

    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

    def initialize(self) -> "PublishingRepository":
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM application_schema WHERE component = ?",
                (_PUBLISHING_SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > _PUBLISHING_SCHEMA_VERSION:
                raise StorageSchemaError(
                    f"publishing schema {version} is newer than supported {_PUBLISHING_SCHEMA_VERSION}"
                )
            if version not in {0, _PUBLISHING_SCHEMA_VERSION}:
                raise StorageSchemaError(
                    f"unsupported publishing schema migration: {version} -> {_PUBLISHING_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS publish_operations (
                    request_sha256 TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS publish_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL REFERENCES publish_operations(request_sha256),
                    attempt_number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    approval_json TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(request_sha256, attempt_number)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_publish_attempts_request ON publish_attempts(request_sha256, attempt_number)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_publish_attempts_state ON publish_attempts(state)"
            )
            if version == 0:
                connection.execute(
                    "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                    (_PUBLISHING_SCHEMA_COMPONENT, _PUBLISHING_SCHEMA_VERSION),
                )
        return self

    @staticmethod
    def _decode_operation(row) -> PublishOperationRecord:
        return PublishOperationRecord(
            request_sha256=str(row["request_sha256"]),
            idempotency_key=str(row["idempotency_key"]),
            request=PublishRequest.model_validate_json(str(row["request_json"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _decode_attempt(row) -> PublishAttemptRecord:
        result = None if row["result_json"] is None else PublishResult.model_validate_json(str(row["result_json"]))
        return PublishAttemptRecord(
            attempt_id=str(row["attempt_id"]),
            request_sha256=str(row["request_sha256"]),
            attempt_number=int(row["attempt_number"]),
            state=str(row["state"]),
            approval=PublishApproval.model_validate_json(str(row["approval_json"])),
            result=result,
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            error_message=None if row["error_message"] is None else str(row["error_message"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            started_at=None if row["started_at"] is None else datetime.fromisoformat(str(row["started_at"])),
            finished_at=None if row["finished_at"] is None else datetime.fromisoformat(str(row["finished_at"])),
        )

    def ensure_operation(self, approved: ApprovedPublishRequest) -> PublishOperationRecord:
        request = approved.request
        digest = semantic_publish_request_digest(request)
        key = publish_idempotency_key(request)
        encoded = _json(request)
        now = _now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM publish_operations WHERE request_sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO publish_operations(request_sha256, idempotency_key, request_json, created_at) VALUES (?, ?, ?, ?)",
                    (digest, key, encoded, now.isoformat()),
                )
                return PublishOperationRecord(
                    request_sha256=digest,
                    idempotency_key=key,
                    request=request,
                    created_at=now,
                )
            existing = self._decode_operation(row)
            if existing.idempotency_key != key or _json(existing.request) != encoded:
                raise StorageConflictError("publish operation identity collision")
            return existing

    def get_operation(self, request_sha256: str) -> PublishOperationRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM publish_operations WHERE request_sha256 = ?",
                (request_sha256,),
            ).fetchone()
        return None if row is None else self._decode_operation(row)

    def attempts(self, request_sha256: str) -> tuple[PublishAttemptRecord, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM publish_attempts WHERE request_sha256 = ? ORDER BY attempt_number",
                (request_sha256,),
            ).fetchall()
        return tuple(self._decode_attempt(row) for row in rows)

    def get_attempt(self, attempt_id: str) -> PublishAttemptRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM publish_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else self._decode_attempt(row)

    def prepare_attempt(self, approved: ApprovedPublishRequest) -> PublishAttemptRecord:
        operation = self.ensure_operation(approved)
        now = _now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT state, attempt_number FROM publish_attempts WHERE request_sha256 = ? ORDER BY attempt_number",
                (operation.request_sha256,),
            ).fetchall()
            states = [str(row["state"]) for row in rows]
            if "succeeded" in states:
                raise StorageConflictError("publish operation already succeeded")
            if "outcome_unknown" in states:
                raise StorageConflictError("publish operation has unresolved remote outcome")
            if any(state in {"prepared", "running"} for state in states):
                raise StorageConflictError("publish operation already has an active attempt")
            number = 1 if not rows else max(int(row["attempt_number"]) for row in rows) + 1
            attempt_id = new_entity_id(EntityKind.JOB)
            connection.execute(
                """
                INSERT INTO publish_attempts(
                    attempt_id, request_sha256, attempt_number, state, approval_json,
                    result_json, error_code, error_message, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, 'prepared', ?, NULL, NULL, NULL, ?, NULL, NULL)
                """,
                (attempt_id, operation.request_sha256, number, _json(approved.approval), now.isoformat()),
            )
        return PublishAttemptRecord(
            attempt_id=attempt_id,
            request_sha256=operation.request_sha256,
            attempt_number=number,
            state="prepared",
            approval=approved.approval,
            created_at=now,
        )

    def mark_running(self, attempt_id: str) -> PublishAttemptRecord:
        now = _now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM publish_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise StorageConflictError("unknown publish attempt")
            current = self._decode_attempt(row)
            if current.state != "prepared":
                raise StorageConflictError(f"publish attempt is {current.state}, expected prepared")
            connection.execute(
                "UPDATE publish_attempts SET state = 'running', started_at = ? WHERE attempt_id = ?",
                (now.isoformat(), attempt_id),
            )
        return current.model_copy(update={"state": "running", "started_at": now})

    def mark_succeeded(self, attempt_id: str, result: PublishResult) -> PublishAttemptRecord:
        now = _now()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM publish_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise StorageConflictError("unknown publish attempt")
            current = self._decode_attempt(row)
            if current.state != "running":
                raise StorageConflictError(f"publish attempt is {current.state}, expected running")
            if result.evidence.request_sha256 != current.request_sha256:
                raise StorageConflictError("publish result does not belong to attempt operation")
            connection.execute(
                "UPDATE publish_attempts SET state = 'succeeded', result_json = ?, finished_at = ? WHERE attempt_id = ?",
                (_json(result), now.isoformat(), attempt_id),
            )
        return current.model_copy(update={"state": "succeeded", "result": result, "finished_at": now})

    def mark_failed(self, attempt_id: str, *, code: str, message: str) -> PublishAttemptRecord:
        return self._mark_error(attempt_id, state="failed", code=code, message=message)

    def mark_outcome_unknown(self, attempt_id: str, *, code: str, message: str) -> PublishAttemptRecord:
        return self._mark_error(attempt_id, state="outcome_unknown", code=code, message=message)

    def _mark_error(
        self,
        attempt_id: str,
        *,
        state: Literal["failed", "outcome_unknown"],
        code: str,
        message: str,
    ) -> PublishAttemptRecord:
        code = code.strip()
        message = message.strip()
        if not code or len(code) > 128 or not message or len(message) > 8192:
            raise ValueError("publish error evidence must be non-empty and bounded")
        now = _now()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM publish_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise StorageConflictError("unknown publish attempt")
            current = self._decode_attempt(row)
            allowed = {"prepared", "running"} if state == "failed" else {"running"}
            if current.state not in allowed:
                raise StorageConflictError(
                    f"publish attempt is {current.state}, cannot transition to {state}"
                )
            connection.execute(
                """
                UPDATE publish_attempts
                SET state = ?, error_code = ?, error_message = ?, finished_at = ?
                WHERE attempt_id = ?
                """,
                (state, code, message, now.isoformat(), attempt_id),
            )
        return current.model_copy(
            update={
                "state": state,
                "error_code": code,
                "error_message": message,
                "finished_at": now,
            }
        )

    def reconcile_running_as_unknown(self) -> int:
        now = _now().isoformat()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE publish_attempts
                SET state = 'outcome_unknown',
                    error_code = 'runtime_interrupted',
                    error_message = 'runtime ended while remote publish outcome was not durably known',
                    finished_at = ?
                WHERE state = 'running'
                """,
                (now,),
            ).rowcount
        return int(changed)


__all__ = [
    "PublishAttemptRecord",
    "PublishAttemptState",
    "PublishOperationRecord",
    "PublishingRepository",
]
