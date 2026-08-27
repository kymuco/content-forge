from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app
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
            "label": "pr9-url-note-recovery",
        },
    )
    assert exchanged.status_code == 200
    return exchanged.json()["token"]


def test_url_note_operational_failure_stays_queued_and_resumes_same_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(
        root=tmp_path,
        ffprobe_path="definitely-missing-ffprobe",
        ffmpeg_path="definitely-missing-ffmpeg",
    )
    client = TestClient(app, raise_server_exceptions=False)
    token = _paired_token(client)
    key = "923e4567-e89b-42d3-a456-426614174008"
    headers = {
        **LOOPBACK_HEADERS,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }
    payload = {
        "source_url": "https://example.com/retryable-note",
        "note": "preserve this capture across a transient project failure",
    }

    original_ensure_project = app.state.inbox._ensure_project

    def fail_project_handoff(_intake):
        raise OSError("synthetic project handoff failure")

    monkeypatch.setattr(app.state.inbox, "_ensure_project", fail_project_handoff)
    try:
        first = client.post(
            "/api/v1/inbox/url-note",
            headers=headers,
            json=payload,
        )
        # A durable RECEIVING recovery checkpoint is an application-layer result, not
        # remote success. The PWA treats 5xx as retryable and keeps its IndexedDB record.
        assert first.status_code == 500
        assert first.json()["detail"] == "URL/note capture awaits recovery"

        intake_id = intake_id_for_key(key)
        receipt = app.state.application_repository.get_intake(intake_id)
        assert receipt is not None
        assert receipt.intake_id == intake_id
        assert receipt.state.value == "receiving"
        assert receipt.error_code == "capture_retryable"
        assert receipt.project_id is None

        monkeypatch.setattr(
            app.state.inbox,
            "_ensure_project",
            original_ensure_project,
        )
        retry = client.post(
            "/api/v1/inbox/url-note",
            headers=headers,
            json=payload,
        )
        assert retry.status_code == 201
        body = retry.json()
        assert body["intake_id"] == intake_id
        assert body["state"] == "prepared"
        assert body["project_id"] is not None
        assert len(app.state.inbox.list_intakes()) == 1
    finally:
        app.state.runtime_lease.close()
