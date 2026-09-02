from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from content_forge.providers import (
    PublishArtifactRef,
    PublishDeclarations,
    PublishInvocationEvidence,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    PublishTarget,
    PublishingProviderHealth,
    PublishingResponseError,
    approve_publish_request,
    publish_idempotency_key,
    semantic_publish_request_digest,
    validate_publish_result,
)

LEGACY_DIGEST = "86e161f3bbb1e8bd4f20f48c5166a41599053c21a06ace128304a6ad67b5831c"


def _artifact() -> PublishArtifactRef:
    return PublishArtifactRef(
        project_id="cf_project_" + "1" * 32,
        render_job_id="cf_job_" + "2" * 32,
        profile_id="youtube_shorts_1080p",
        variant_id=None,
        render_plan_digest="3" * 64,
        output_sha256="4" * 64,
        bytes_written=1234,
        width=1080,
        height=1920,
        duration_seconds=12.5,
        has_audio=True,
    )


def _metadata() -> PublishMetadata:
    return PublishMetadata(
        title="Legacy title",
        description="Legacy description",
        tags=("one", "two"),
        visibility="private",
    )


def _target() -> PublishTarget:
    return PublishTarget(
        provider_id="youtube",
        destination_id="UC1234567890123456789012",
    )


def test_pr29_v1_digest_is_frozen_across_model_extension() -> None:
    request = PublishRequest(
        artifact=_artifact(),
        target=_target(),
        metadata=_metadata(),
    )
    assert request.contract_version == "pr27_publish_contract_v1"
    assert request.declarations is None
    assert semantic_publish_request_digest(request) == LEGACY_DIGEST
    assert publish_idempotency_key(request) == f"cfp-{LEGACY_DIGEST}"

    legacy_json_without_new_fields = {
        "artifact": _artifact().model_dump(mode="json"),
        "target": _target().model_dump(mode="json"),
        "metadata": _metadata().model_dump(mode="json"),
    }
    decoded = PublishRequest.model_validate(legacy_json_without_new_fields)
    assert decoded.contract_version == "pr27_publish_contract_v1"
    assert semantic_publish_request_digest(decoded) == LEGACY_DIGEST

    approved = approve_publish_request(
        decoded,
        approved_at=datetime(2026, 9, 2, 9, 30, tzinfo=timezone.utc),
    )
    assert approved.approval.contract_version == "pr27_publish_contract_v1"
    assert approved.approval.request_sha256 == LEGACY_DIGEST


def test_pr29_v2_requires_both_explicit_boolean_declarations() -> None:
    with pytest.raises(ValidationError, match="require explicit publication declarations"):
        PublishRequest(
            contract_version="pr29_publish_contract_v2",
            artifact=_artifact(),
            target=_target(),
            metadata=_metadata(),
        )

    with pytest.raises(ValidationError, match="Field required"):
        PublishDeclarations(child_directed=False)

    with pytest.raises(ValidationError, match="v1 publish requests cannot contain"):
        PublishRequest(
            artifact=_artifact(),
            target=_target(),
            metadata=_metadata(),
            declarations=PublishDeclarations(
                child_directed=False,
                contains_realistic_altered_or_synthetic_media=False,
            ),
        )


def test_pr29_v2_declarations_participate_in_exact_digest_and_approval() -> None:
    base = dict(
        contract_version="pr29_publish_contract_v2",
        artifact=_artifact(),
        target=_target(),
        metadata=_metadata(),
    )
    ordinary = PublishRequest(
        **base,
        declarations=PublishDeclarations(
            child_directed=False,
            contains_realistic_altered_or_synthetic_media=False,
        ),
    )
    synthetic = PublishRequest(
        **base,
        declarations=PublishDeclarations(
            child_directed=False,
            contains_realistic_altered_or_synthetic_media=True,
        ),
    )
    kids = PublishRequest(
        **base,
        declarations=PublishDeclarations(
            child_directed=True,
            contains_realistic_altered_or_synthetic_media=False,
        ),
    )

    digests = {
        semantic_publish_request_digest(ordinary),
        semantic_publish_request_digest(synthetic),
        semantic_publish_request_digest(kids),
    }
    assert len(digests) == 3
    assert LEGACY_DIGEST not in digests
    assert publish_idempotency_key(ordinary) != publish_idempotency_key(synthetic)

    approved = approve_publish_request(
        synthetic,
        approved_at=datetime(2026, 9, 2, 9, 31, tzinfo=timezone.utc),
    )
    assert approved.approval.contract_version == "pr29_publish_contract_v2"
    assert approved.approval.request_sha256 == semantic_publish_request_digest(synthetic)


def test_pr29_result_evidence_must_pin_the_same_contract_version() -> None:
    request = PublishRequest(
        contract_version="pr29_publish_contract_v2",
        artifact=_artifact(),
        target=_target(),
        metadata=_metadata(),
        declarations=PublishDeclarations(
            child_directed=False,
            contains_realistic_altered_or_synthetic_media=True,
        ),
    )
    approved = approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 2, 9, 32, tzinfo=timezone.utc),
    )
    health = PublishingProviderHealth(
        provider_id="youtube",
        provider_version="fixture",
        available=True,
    )
    result = PublishResult(
        disposition="published",
        remote_id="video_123",
        effective_at=datetime(2026, 9, 2, 9, 33, tzinfo=timezone.utc),
        evidence=PublishInvocationEvidence(
            contract_version="pr27_publish_contract_v1",
            provider_id="youtube",
            provider_version="fixture",
            request_sha256=semantic_publish_request_digest(request),
            idempotency_key=publish_idempotency_key(request),
            output_sha256=request.artifact.output_sha256,
            destination_id=request.target.destination_id,
        ),
    )
    with pytest.raises(PublishingResponseError, match="contract version"):
        validate_publish_result(approved, health, result)
