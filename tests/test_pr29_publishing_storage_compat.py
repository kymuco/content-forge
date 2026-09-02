from __future__ import annotations

import json
from datetime import datetime, timezone

from content_forge.providers import semantic_publish_request_digest
from content_forge.storage import LocalLibrary

LEGACY_DIGEST = "86e161f3bbb1e8bd4f20f48c5166a41599053c21a06ace128304a6ad67b5831c"
ATTEMPT_ID = "cf_publish_" + "5" * 32


def _legacy_request_json() -> str:
    payload = {
        "artifact": {
            "project_id": "cf_project_" + "1" * 32,
            "render_job_id": "cf_job_" + "2" * 32,
            "profile_id": "youtube_shorts_1080p",
            "variant_id": None,
            "render_plan_digest": "3" * 64,
            "output_sha256": "4" * 64,
            "bytes_written": 1234,
            "width": 1080,
            "height": 1920,
            "duration_seconds": 12.5,
            "has_audio": True,
        },
        "target": {
            "provider_id": "youtube",
            "destination_id": "UC1234567890123456789012",
        },
        "metadata": {
            "title": "Legacy title",
            "description": "Legacy description",
            "tags": ["one", "two"],
            "visibility": "private",
            "scheduled_for": None,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _legacy_approval_json() -> str:
    return json.dumps(
        {
            "contract_version": "pr27_publish_contract_v1",
            "request_sha256": LEGACY_DIGEST,
            "approved_at": "2026-09-02T09:30:00Z",
            "note": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_pr29_repository_loads_pre_v2_request_json_and_preserves_identity(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "library")
    repository = library.publishing
    created = datetime(2026, 9, 2, 9, 30, tzinfo=timezone.utc).isoformat()

    with library.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO publish_operations(
                request_sha256, idempotency_key, request_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                LEGACY_DIGEST,
                f"cfp-{LEGACY_DIGEST}",
                _legacy_request_json(),
                created,
            ),
        )
        connection.execute(
            """
            INSERT INTO publish_attempts(
                attempt_id, request_sha256, attempt_number, state, approval_json,
                provider_health_json, result_json, error_code, error_message,
                created_at, started_at, finished_at
            ) VALUES (?, ?, 1, 'prepared', ?, NULL, NULL, NULL, NULL, ?, NULL, NULL)
            """,
            (ATTEMPT_ID, LEGACY_DIGEST, _legacy_approval_json(), created),
        )

    operation = repository.get_operation(LEGACY_DIGEST)
    assert operation is not None
    assert operation.request.contract_version == "pr27_publish_contract_v1"
    assert operation.request.declarations is None
    assert semantic_publish_request_digest(operation.request) == LEGACY_DIGEST

    approved = repository.approved_request(ATTEMPT_ID)
    assert approved.request.contract_version == "pr27_publish_contract_v1"
    assert approved.approval.contract_version == "pr27_publish_contract_v1"
    assert approved.approval.request_sha256 == LEGACY_DIGEST

    # Re-ensuring the historical operation normalizes the old JSON through the
    # current model and must not create an identity collision after the upgrade.
    same = repository.ensure_operation(approved)
    assert same.request_sha256 == LEGACY_DIGEST
    assert same.idempotency_key == f"cfp-{LEGACY_DIGEST}"
