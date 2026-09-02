from __future__ import annotations

import pytest
from pydantic import ValidationError

from content_forge.api.publishing_routes import PublishCandidateInput

RENDER_JOB_ID = "cf_job_" + "1" * 32
TARGET = {
    "provider_id": "youtube",
    "destination_id": "UC1234567890123456789012",
}
METADATA = {
    "title": "Approved upload",
    "description": "description",
    "tags": ["one", "two"],
    "visibility": "private",
}


def test_pr29_candidate_transport_keeps_legacy_v1_input_compatible() -> None:
    candidate = PublishCandidateInput.model_validate(
        {
            "render_job_id": RENDER_JOB_ID,
            "target": TARGET,
            "metadata": METADATA,
        }
    )
    assert candidate.contract_version == "pr27_publish_contract_v1"
    assert candidate.declarations is None


def test_pr29_candidate_transport_requires_declarations_for_v2() -> None:
    with pytest.raises(ValidationError, match="require explicit publication declarations"):
        PublishCandidateInput.model_validate(
            {
                "render_job_id": RENDER_JOB_ID,
                "target": TARGET,
                "metadata": METADATA,
                "contract_version": "pr29_publish_contract_v2",
            }
        )

    candidate = PublishCandidateInput.model_validate(
        {
            "render_job_id": RENDER_JOB_ID,
            "target": TARGET,
            "metadata": METADATA,
            "contract_version": "pr29_publish_contract_v2",
            "declarations": {
                "child_directed": False,
                "contains_realistic_altered_or_synthetic_media": True,
            },
        }
    )
    assert candidate.declarations is not None
    assert candidate.declarations.child_directed is False
    assert candidate.declarations.contains_realistic_altered_or_synthetic_media is True


def test_pr29_candidate_transport_rejects_declarations_on_v1() -> None:
    with pytest.raises(ValidationError, match="v1 publish candidates cannot contain"):
        PublishCandidateInput.model_validate(
            {
                "render_job_id": RENDER_JOB_ID,
                "target": TARGET,
                "metadata": METADATA,
                "declarations": {
                    "child_directed": False,
                    "contains_realistic_altered_or_synthetic_media": False,
                },
            }
        )
