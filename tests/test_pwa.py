from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.web import static_path
from content_forge.web.onboarding import normalize_public_base_url

LOOPBACK_HEADERS = {"Host": "localhost"}


def test_pwa_shell_manifest_icons_and_security_headers(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/", headers=LOOPBACK_HEADERS)
        assert shell.status_code == 200
        assert 'rel="manifest" href="manifest.webmanifest"' in shell.text
        assert "default-src 'self'" in shell.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in shell.headers["content-security-policy"]
        assert shell.headers["referrer-policy"] == "no-referrer"
        assert shell.headers["x-content-type-options"] == "nosniff"

        manifest_response = client.get("/app/manifest.webmanifest", headers=LOOPBACK_HEADERS)
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        assert manifest["display"] == "standalone"
        assert manifest["start_url"] == "./"
        assert manifest["scope"] == "./"
        assert manifest["share_target"] == {
            "action": "share-target",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url",
                "files": [
                    {
                        "name": "files",
                        "accept": ["image/*", "video/*", "audio/*"],
                    }
                ],
            },
        }

        for name, size in (("icon-192.png", 192), ("icon-512.png", 512)):
            icon = client.get(f"/app/icons/{name}", headers=LOOPBACK_HEADERS)
            assert icon.status_code == 200
            assert icon.headers["content-type"] == "image/png"
            assert icon.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert static_path(f"icons/{name}").is_file()
            assert f'"sizes": "{size}x{size}"' in static_path("manifest.webmanifest").read_text()
    finally:
        app.state.runtime_lease.close()


def test_service_worker_and_client_preserve_share_queue_and_authenticated_upload_boundary() -> None:
    service_worker = static_path("sw.js").read_text(encoding="utf-8")
    client = static_path("app.js").read_text(encoding="utf-8")
    shared = static_path("shared.js").read_text(encoding="utf-8")

    assert 'request.method === "POST"' in service_worker
    assert 'request.formData()' in service_worker
    assert "enqueueShare" in service_worker
    assert "Response.redirect" in service_worker
    assert "indexedDB.open" in shared
    assert 'const TOKEN_KEY = "bearer-token"' in shared
    assert "XMLHttpRequest" in client
    assert 'setRequestHeader("Authorization"' in client
    assert "xhr.upload.onprogress" in client
    assert "replaceChildren" in client
    assert "innerHTML" not in client


def test_server_share_target_fallback_never_ingests_and_is_preparse_bounded(tmp_path) -> None:
    app = create_app(root=tmp_path, max_upload_bytes=32)
    client = TestClient(app)
    try:
        fallback = client.post(
            "/app/share-target",
            headers={"Host": "localhost", "Content-Type": "multipart/form-data; boundary=cf"},
            content=b"opaque-share-body",
        )
        assert fallback.status_code == 409
        assert app.state.inbox.list_intakes() == ()

        too_large = client.build_request(
            "POST",
            "/app/share-target",
            headers={"Host": "localhost", "Content-Type": "multipart/form-data; boundary=cf"},
            content=b"x",
        )
        too_large.headers["content-length"] = str(2 * 1024 * 1024)
        rejected = client.send(too_large)
        assert rejected.status_code == 413
        assert app.state.inbox.list_intakes() == ()

        missing_length = client.build_request(
            "POST",
            "/app/share-target",
            headers={"Host": "localhost", "Content-Type": "multipart/form-data; boundary=cf"},
            content=b"x",
        )
        del missing_length.headers["content-length"]
        rejected = client.send(missing_length)
        assert rejected.status_code == 411
        assert app.state.inbox.list_intakes() == ()
    finally:
        app.state.runtime_lease.close()


def test_loopback_onboarding_builds_fragment_only_pairing_qr_and_session(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/v1/pairing/challenges",
            headers=LOOPBACK_HEADERS,
            params={"public_url": "https://192.168.50.10:8765"},
        )
        assert created.status_code == 201
        payload = created.json()
        parsed = urlsplit(payload["pairing_url"])
        assert parsed.scheme == "https"
        assert parsed.netloc == "192.168.50.10:8765"
        assert parsed.path == "/app/"
        assert parsed.query == ""
        fragment = parse_qs(parsed.fragment)
        assert fragment["challenge_id"] == [payload["challenge_id"]]
        assert fragment["code"] == [payload["code"]]
        assert payload["code"] not in parsed.path
        assert payload["code"] not in parsed.query
        assert payload["qr_svg"].lstrip().startswith("<svg")

        exchanged = client.post(
            "/api/v1/pairing/exchange",
            headers=LOOPBACK_HEADERS,
            json={
                "challenge_id": payload["challenge_id"],
                "code": payload["code"],
                "label": "pwa-regression",
            },
        )
        assert exchanged.status_code == 200
        token = exchanged.json()["token"]
        inbox = client.get(
            "/api/v1/inbox",
            headers={**LOOPBACK_HEADERS, "Authorization": f"Bearer {token}"},
        )
        assert inbox.status_code == 200
    finally:
        app.state.runtime_lease.close()


def test_onboarding_rejects_plaintext_lan_and_hostile_browser_authority(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        plaintext = client.post(
            "/api/v1/pairing/challenges",
            headers=LOOPBACK_HEADERS,
            params={"public_url": "http://192.168.50.10:8765"},
        )
        assert plaintext.status_code == 422

        hostile_origin = client.post(
            "/api/v1/pairing/challenges",
            headers={"Host": "localhost", "Origin": "https://attacker.example"},
            params={"public_url": "https://192.168.50.10:8765"},
        )
        assert hostile_origin.status_code == 403

        assert normalize_public_base_url("http://localhost:8765/") == "http://localhost:8765"
    finally:
        app.state.runtime_lease.close()


def test_pwa_shell_and_onboarding_remain_mount_relative(tmp_path) -> None:
    child = create_app(root=tmp_path)
    parent = FastAPI()
    parent.mount("/content-forge", child)
    client = TestClient(parent)
    try:
        shell = client.get("/content-forge/app/", headers=LOOPBACK_HEADERS)
        assert shell.status_code == 200
        manifest = client.get(
            "/content-forge/app/manifest.webmanifest",
            headers=LOOPBACK_HEADERS,
        )
        assert manifest.status_code == 200
        assert manifest.json()["start_url"] == "./"

        onboarding = client.post(
            "/content-forge/api/v1/pairing/challenges",
            headers=LOOPBACK_HEADERS,
            params={"public_url": "https://forge.local:8765/content-forge"},
        )
        assert onboarding.status_code == 201
        assert onboarding.json()["pairing_url"].startswith(
            "https://forge.local:8765/content-forge/app/#"
        )
    finally:
        child.state.runtime_lease.close()
