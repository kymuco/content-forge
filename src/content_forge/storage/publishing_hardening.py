"""PR27 privacy and restart hardening for the public publishing repository."""

from __future__ import annotations

from content_forge.providers.publishing import PublishingProviderHealth

from .publishing import PublishingRepository as _BasePublishingRepository


class PublishingRepository(_BasePublishingRepository):
    """Public repository with redacted provider health and explicit restart recovery."""

    def mark_running(self, attempt_id: str, health: PublishingProviderHealth):
        safe_health = health.model_copy(update={"reason": None})
        return super().mark_running(attempt_id, safe_health)

    def reconcile_interrupted(self) -> int:
        """Retire only attempts whose remote execution may already have begun.

        `prepared` is a durable approved state and remains safely resumable across
        restarts. Only `running` crosses the remote-side-effect boundary, so only it is
        conservatively converted to retry-blocking `outcome_unknown`.
        """

        return super().reconcile_running_as_unknown()


__all__ = ["PublishingRepository"]
