"""PR27 privacy hardening for the public durable publishing repository."""

from __future__ import annotations

from content_forge.providers.publishing import PublishingProviderHealth

from .publishing import PublishingRepository as _BasePublishingRepository


class PublishingRepository(_BasePublishingRepository):
    """Public repository that never persists provider-controlled health reasons."""

    def mark_running(self, attempt_id: str, health: PublishingProviderHealth):
        safe_health = health.model_copy(update={"reason": None})
        return super().mark_running(attempt_id, safe_health)


__all__ = ["PublishingRepository"]
