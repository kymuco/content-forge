"""PR27 fail-closed validation for publishing-provider results."""

from __future__ import annotations

from .publishing import (
    ApprovedPublishRequest,
    PublishResult,
    PublishingProviderHealth,
    PublishingResponseError,
    publish_idempotency_key,
    semantic_publish_request_digest,
)


def validate_publish_result(
    request: ApprovedPublishRequest,
    health: PublishingProviderHealth,
    result: PublishResult,
) -> PublishResult:
    """Bind one remote result to the exact approved request and provider identity."""

    if not health.available:
        raise PublishingResponseError("publishing provider reported unavailable before result validation")
    target = request.request.target
    artifact = request.request.artifact
    expected_request_sha256 = semantic_publish_request_digest(request.request)
    expected_idempotency_key = publish_idempotency_key(request.request)

    if target.provider_id != health.provider_id:
        raise PublishingResponseError("publish target provider does not match provider health identity")
    if result.evidence.provider_id != health.provider_id:
        raise PublishingResponseError("publish result provider identity does not match provider health")
    if result.evidence.provider_version != health.provider_version:
        raise PublishingResponseError("publish result provider version does not match provider health")
    if result.evidence.request_sha256 != expected_request_sha256:
        raise PublishingResponseError("publish result request digest does not match approved request")
    if result.evidence.request_sha256 != request.approval.request_sha256:
        raise PublishingResponseError("publish result request digest does not match approval evidence")
    if result.evidence.idempotency_key != expected_idempotency_key:
        raise PublishingResponseError("publish result idempotency key does not match approved request")
    if result.evidence.output_sha256 != artifact.output_sha256:
        raise PublishingResponseError("publish result output digest does not match approved artifact")
    if result.evidence.destination_id != target.destination_id:
        raise PublishingResponseError("publish result destination does not match approved target")

    expected_disposition = (
        "scheduled" if request.request.metadata.scheduled_for is not None else "published"
    )
    if result.disposition != expected_disposition:
        raise PublishingResponseError(
            f"publish result disposition must be {expected_disposition} for the approved request"
        )
    return result


__all__ = ["validate_publish_result"]
