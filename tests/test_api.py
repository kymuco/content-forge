from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from content_forge.api import create_app

LOOPBACK_HEADERS = {"Host": "localhost"}


def _paired_client(tmp_path) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(create_app(root=tmp_path))
    challenge = client.post(
        "/api/v1/pairing/challenges",
        headers=LOOPBACK_HEADERS,
    )
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


def _programmatic_lan_get(app, *, base_url: str):
    async def request():
        transport = httpx.ASGITransport(
            app=app,
            client=("192.168.50.23", 48152),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
        ) as client:
            return await client.get("/health")

    return asyncio.run(request())


def test_programmatic_asgi_lan_plaintext_is_rejected_but_https_is_allowed(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        plaintext = _programmatic_lan_get(
            app,
            base_url="http://content-forge.lan",
        )
        assert plaintext.status_code == 426
        assert plaintext.json()["detail"] == "non-loopback requests require HTTPS"

        encrypted = _programmatic_lan_get(
            app,
            base_url="https://content-forge.lan",
        )
        assert encrypted.status_code == 200
        assert encrypted.json()["ok"] is True
    finally:
        app.state.runtime_lease.close()


def test_pairing_challenge_rejects_nonloopback_browser_authority(tmp_path) -> None:
    client = TestClient(create_app(root=tmp_path))
    assert client.post(
        "/api/v1/pairing/challenges",
        headers={"Host": "attacker.example"},
    ).status_code == 403
    assert client.post(
        "/api/v1/pairing/challenges",
        headers={"Host": "localhost", "Origin": "https://attacker.example"},
    ).status_code == 403
    assert client.post(
        "/api/v1/pairing/challenges",
        headers={"Host": "localhost", "Origin": "http://127.0.0.1:8765"},
    ).status_code == 201


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
