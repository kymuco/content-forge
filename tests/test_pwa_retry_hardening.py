from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import InboxIntake, IntakeKind, IntakeState, PreparationState
from content_forge.application.idempotency import intake_id_for_key

LOOPBACK_HEADERS = {"Host": "localhost"}


def _paired_token(client: TestClient) -> str:
    challenge = client.post("/api/v1/pairing/challenges", headers=LOOPBACK_HEADERS)
    assert challenge.status_code == 201
    payload = challenge.json()
    exchanged = client.post(
        "/api/v1/pairing/exchange",
        headers=LOOPBACK_HEADERS,
        json={
            "challenge_id": payload["challenge_id"],
            "code": payload["code"],
            "label": "retry-hardening",
        },
    )
    assert exchanged.status_code == 200
    return exchanged.json()["token"]


def test_permission_error_before_byte_acceptance_revives_same_intake(tmp_path) -> None:
    app = create_app(
        root=tmp_path,
        ffprobe_path="definitely-missing-ffprobe",
        ffmpeg_path="definitely-missing-ffmpeg",
        max_upload_bytes=64,
    )
    client = TestClient(app)
    try:
        token = _paired_token(client)
        auth = {**LOOPBACK_HEADERS, "Authorization": f"Bearer {token}"}
        key = "623e4567-e89b-42d3-a456-426614174005"
        intake_id = intake_id_for_key(key)
        repository = app.state.application_repository
        repository.create_intake(
            InboxIntake(
                intake_id=intake_id,
                kind=IntakeKind.FILE,
                original_name="permission.bin",
                mime_type="application/octet-stream",
            )
        )
        failed = repository.transition_intake(
            intake_id,
            expected_state=IntakeState.RECEIVING,
            update={
                "state": IntakeState.FAILED,
                "probe_state": PreparationState.SKIPPED,
                "thumbnail_state": PreparationState.SKIPPED,
                "error_code": "PermissionError",
                "error_message": "temporary staging permission failure",
            },
        )
        assert failed.content_sha256 is None
        assert failed.asset_id is None
        assert failed.project_id is None

        resumed = client.post(
            "/api/v1/inbox/files",
            headers={**auth, "Idempotency-Key": key},
            files={
                "file": (
                    "permission.bin",
                    b"bytes accepted after permission repair",
                    "application/octet-stream",
                )
            },
        )
        assert resumed.status_code == 201
        payload = resumed.json()
        assert payload["intake_id"] == intake_id
        assert payload["size_bytes"] == len(b"bytes accepted after permission repair")
        assert payload["content_sha256"] is not None
        assert payload["state"] == "partial"
        assert payload["error_code"] == "media_probe_failed"
    finally:
        app.state.runtime_lease.close()
