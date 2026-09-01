"""PR27 privacy and restart hardening for the public publishing repository."""

from __future__ import annotations

from datetime import datetime, timezone

from content_forge.providers.publishing import PublishingProviderHealth

from .publishing import PublishingRepository as _BasePublishingRepository


class PublishingRepository(_BasePublishingRepository):
    """Public repository with redacted health evidence and crash-safe restart policy."""

    def mark_running(self, attempt_id: str, health: PublishingProviderHealth):
        safe_health = health.model_copy(update={"reason": None})
        return super().mark_running(attempt_id, safe_health)

    def reconcile_interrupted(self) -> int:
        """Recover orphan attempts according to whether remote execution may have begun."""

        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            prepared = connection.execute(
                """
                UPDATE publish_attempts
                SET state = 'failed',
                    error_code = 'runtime_interrupted_preflight',
                    error_message = 'runtime ended before remote publish execution began',
                    finished_at = ?
                WHERE state = 'prepared'
                """,
                (now,),
            ).rowcount
        running = super().reconcile_running_as_unknown()
        return int(prepared) + int(running)


__all__ = ["PublishingRepository"]
