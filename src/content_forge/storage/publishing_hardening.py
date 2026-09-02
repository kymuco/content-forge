"""PR27 privacy and restart hardening for the public publishing repository."""

from __future__ import annotations

from typing import Literal

from content_forge.providers.publishing import PublishingProviderHealth

from .database import StorageConflictError
from .publishing import PublishingRepository as _BasePublishingRepository
from .publishing import _now


class PublishingRepository(_BasePublishingRepository):
    """Public repository with redacted health evidence and crash-safe state transitions."""

    def mark_running(self, attempt_id: str, health: PublishingProviderHealth):
        safe_health = health.model_copy(update={"reason": None})
        return super().mark_running(attempt_id, safe_health)

    def _mark_error(
        self,
        attempt_id: str,
        *,
        state: Literal["failed", "outcome_unknown"],
        code: str,
        message: str,
    ):
        """Fail closed around the remote-side-effect boundary.

        `failed` is retryable evidence that no remote call may have happened, so only a
        `prepared` attempt may enter it. Once an attempt is `running`, the only error
        terminal state is `outcome_unknown`.
        """

        code = code.strip()
        message = message.strip()
        if not code or len(code) > 128 or not message or len(message) > 8192:
            raise ValueError("publish error evidence must be non-empty and bounded")
        now = _now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM publish_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise StorageConflictError("unknown publish attempt")
            current = self._decode_attempt(row)
            allowed = {"prepared"} if state == "failed" else {"running"}
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

    def reconcile_interrupted(self) -> int:
        """Retire only attempts whose remote execution may already have begun.

        `prepared` is a durable approved state and remains safely resumable across
        restarts. Only `running` crosses the remote-side-effect boundary, so only it is
        conservatively converted to retry-blocking `outcome_unknown`.
        """

        return super().reconcile_running_as_unknown()


__all__ = ["PublishingRepository"]
