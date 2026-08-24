from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


def _paired_client(tmp_path) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(create_app(root=tmp_path))
    challenge = client.post("/api/v1/pairing/challenges")
    assert challenge.status_code == 201
    payload = challenge.json()
    exchanged = client.post(
        "/api/v1/pairing/exchange",
        json={
            "challenge_id": payload["challenge_id"],
            "code": payload["code"],
            "label": "pytest",
        },
    )
    assert exchanged.status_code == 200
    token = exchanged.json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_sensitive_reads_and_uploads_require_authentication(tmp_path) -> None:
    client = TestClient(create_app(root=tmp_path))
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/inbox").status_code == 401
    blocked_upload = client.post(
        "/api/v1/inbox/files",
        files={"file": ("x.bin", b"x", "application/octet-stream")},
    )
    assert blocked_upload.status_code == 401


def test_url_note_round_trip_and_session_revocation(tmp_path) -> None:
    client, headers = _paired_client(tmp_path)
    created = client.post(
        "/api/v1/inbox/url-note",
        headers=headers,
        json={"source_url": "https://example.invalid/x", "note": "remember this"},
    )
    assert created.status_code == 201
    intake = created.json()
    assert intake["state"] == "prepared"
    assert intake["asset_id"] is None

    listed = client.get("/api/v1/inbox", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["intake_id"] == intake["intake_id"]

    revoked = client.delete("/api/v1/sessions/current", headers=headers)
    assert revoked.status_code == 204
    assert client.get("/api/v1/inbox", headers=headers).status_code == 401
