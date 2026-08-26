from __future__ import annotations

import json
from threading import Event, Thread
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import InboxIntake, IntakeKind, IntakeState, PreparationState
from content_forge.application.idempotency import (
    intake_id_for_key,
    intake_idempotency_scope,
)
from content_forge.web import static_path
from content_forge.web.onboarding import normalize_public_base_url

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
            "label": "pwa-regression",
        },
    )
    assert exchanged.status_code == 200
    return exchanged.json()["token"]


def _pwa_config(response) -> dict[str, int]:
    prefix = "self.CF_CONFIG = Object.freeze("
    suffix = ");\n"
    assert response.text.startswith(prefix)
    assert response.text.endswith(suffix)
    payload = json.loads(response.text[len(prefix) : -len(suffix)])
    assert isinstance(payload, dict)
    return payload


def test_pwa_shell_manifest_icons_and_security_headers(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/", headers=LOOPBACK_HEADERS)
        assert shell.status_code == 200
        assert 'rel="manifest" href="manifest.webmanifest"' in shell.text
        assert '<script src="config.js"></script>' in shell.text
        assert shell.text.index('src="config.js"') < shell.text.index('src="shared.js"')
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

    assert 'importScripts("config.js", "shared.js")' in service_worker
    assert 'request.method === "POST"' in service_worker
    assert 'request.headers.get("content-length")' in service_worker
    assert 'request.headers.get("sec-fetch-site")' in service_worker
    assert 'request.destination === "document"' in service_worker
    assert "const LIMITS = self.CFStore.limits" in service_worker
    assert "2 * 1024 * 1024 * 1024" not in service_worker
    assert "self.CFStore.getToken()" in service_worker
    assert "self.CFStore.queueUsage()" in service_worker
    assert 'request.formData()' in service_worker
    assert service_worker.index("boundedContentLength(request)") < service_worker.index("request.formData()")
    assert service_worker.index("self.CFStore.getToken()") < service_worker.index("request.formData()")
    assert "body = await response.text()" in service_worker
    assert "payload = JSON.parse(body)" in service_worker
    assert service_worker.index("body = await response.text()") < service_worker.index("payload = JSON.parse(body)")
    assert "await self.CFStore.enqueueShares(records)" in service_worker
    assert "await self.CFStore.enqueueSharesWithLimits(records, activeLimits)" in service_worker
    assert service_worker.index("const activeLimits = await currentShareLimits()") < service_worker.index(
        "enqueueSharesWithLimits(records, activeLimits)"
    )
    assert "Response.redirect" in service_worker
    assert "key.startsWith(CACHE_PREFIX)" in service_worker
    assert 'cache.match(appUrl("./"))' in service_worker
    assert 'appUrl("config.js")' in service_worker

    assert "indexedDB.open" in shared
    assert 'const TOKEN_KEY = "bearer-token"' in shared
    assert "root.CF_CONFIG" in shared
    assert "encodeURIComponent(scopeUrl.pathname)" in shared
    assert "validateQueueMutation" in shared
    assert "maxUploadBytes" in shared
    assert "maxQueueBytes" in shared
    assert "maxQueueEntries" in shared
    assert "async function enqueueSharesWithLimits(records, authority)" in shared
    assert "records.map((record) => normalizeShare(record, limits))" in shared
    assert "validateQueueMutation(Array.isArray(read.result) ? read.result : [], entries, limits)" in shared
    assert "async function enqueueShares(records)" in shared
    assert "for (const entry of entries) store.add(entry)" in shared
    assert "const entries = await enqueueShares([record])" in shared

    assert "async function revokeIssuedPairingToken(token)" in client
    assert "const issuedToken = payload.token" in client
    assert "await window.CFStore.setToken(issuedToken)" in client
    assert "const revoked = await revokeIssuedPairingToken(issuedToken)" in client
    assert client.index("await window.CFStore.setToken(issuedToken)") < client.index("bearerToken = issuedToken;")
    assert "Automatic revocation also failed" in client
    assert "XMLHttpRequest" in client
    assert 'setRequestHeader("Authorization"' in client
    assert 'setRequestHeader("Idempotency-Key", record.id)' in client
    assert '"Idempotency-Key": record.id' in client
    assert "xhr.upload.onprogress" in client
    assert "isPermanentQueueRejection" in client
    assert "![401, 408, 425, 429].includes(value)" in client
    assert "Removed from the retry queue" in client
    assert "Session retained so revocation can be retried" in client
    assert "Nothing was queued; the current selection is still available." in client
    assert "Nothing was queued; your form values were kept." in client
    assert client.index("await window.CFStore.enqueueShares(records)") < client.index('elements.fileInput.value = "";')
    assert "finally { bearerToken = null" not in client
    assert "replaceChildren" in client
    assert "innerHTML" not in client


def test_server_share_target_fallback_never_ingests_and_is_preparse_bounded(tmp_path) -> None:
    app = create_app(root=tmp_path, max_upload_bytes=32)
    client = TestClient(app)
    try:
        config_response = client.get("/app/config.js", headers=LOOPBACK_HEADERS)
        assert config_response.status_code == 200
        config = _pwa_config(config_response)
        assert config["maxUploadBytes"] == 32
        assert config["maxQueueBytes"] == 32
        assert config["maxShareBodyBytes"] == 32 + 1024 * 1024
        assert config["maxQueueEntries"] == 256
        assert config["maxBatchEntries"] == 16

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


def test_idempotency_scope_serializes_same_live_request_key() -> None:
    key = "323e4567-e89b-42d3-a456-426614174002"
    first_entered = Event()
    release_first = Event()
    second_attempting = Event()
    second_entered = Event()

    def first() -> None:
        with intake_idempotency_scope(key):
            first_entered.set()
            release_first.wait()

    def second() -> None:
        first_entered.wait()
        second_attempting.set()
        with intake_idempotency_scope(key):
            second_entered.set()

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    assert first_entered.wait(timeout=2)
    second_thread.start()
    assert second_attempting.wait(timeout=2)
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    assert second_entered.wait(timeout=2)
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()


def test_pwa_queue_idempotency_replays_one_durable_intake_per_record(tmp_path) -> None:
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

        note_key = "123e4567-e89b-42d3-a456-426614174000"
        note_headers = {**auth, "Idempotency-Key": note_key}
        first_note = client.post(
            "/api/v1/inbox/url-note",
            headers=note_headers,
            json={"source_url": "https://example.test/item", "note": "queued once"},
        )
        second_note = client.post(
            "/api/v1/inbox/url-note",
            headers=note_headers,
            json={"source_url": "https://example.test/item", "note": "queued once"},
        )
        assert first_note.status_code == 201
        assert second_note.status_code == 201
        assert second_note.json()["intake_id"] == first_note.json()["intake_id"]
        assert second_note.json()["project_id"] == first_note.json()["project_id"]

        conflict = client.post(
            "/api/v1/inbox/url-note",
            headers=note_headers,
            json={"source_url": "https://example.test/other", "note": "different"},
        )
        assert conflict.status_code == 409

        file_key = "223e4567-e89b-42d3-a456-426614174001"
        file_headers = {**auth, "Idempotency-Key": file_key}
        first_file = client.post(
            "/api/v1/inbox/files",
            headers=file_headers,
            files={"file": ("retry.bin", b"same durable bytes", "application/octet-stream")},
        )
        second_file = client.post(
            "/api/v1/inbox/files",
            headers=file_headers,
            files={"file": ("retry.bin", b"same durable bytes", "application/octet-stream")},
        )
        assert first_file.status_code == 201
        assert second_file.status_code == 201
        assert second_file.json()["intake_id"] == first_file.json()["intake_id"]
        assert second_file.json()["project_id"] == first_file.json()["project_id"]

        different_bytes = client.post(
            "/api/v1/inbox/files",
            headers=file_headers,
            files={"file": ("retry.bin", b"different file bytes", "application/octet-stream")},
        )
        assert different_bytes.status_code == 409
        assert "different file bytes" in different_bytes.json()["detail"]

        inbox = client.get("/api/v1/inbox?limit=100", headers=auth)
        assert inbox.status_code == 200
        assert len(inbox.json()["items"]) == 2

        failed_key = "423e4567-e89b-42d3-a456-426614174003"
        failed_headers = {**auth, "Idempotency-Key": failed_key}
        first_failed = client.post(
            "/api/v1/inbox/files",
            headers=failed_headers,
            files={"file": ("too-large.bin", b"x" * 65, "application/octet-stream")},
        )
        replay_failed = client.post(
            "/api/v1/inbox/files",
            headers=failed_headers,
            files={"file": ("too-large.bin", b"x" * 65, "application/octet-stream")},
        )
        assert first_failed.status_code == 413
        assert replay_failed.status_code == 413
        assert replay_failed.json()["detail"] == "upload exceeds 64 bytes"

        invalid_key = client.post(
            "/api/v1/inbox/url-note",
            headers={**auth, "Idempotency-Key": "not-a-uuid"},
            json={"note": "invalid key"},
        )
        assert invalid_key.status_code == 422
    finally:
        app.state.runtime_lease.close()


def test_idempotent_file_retry_revives_interrupted_preacceptance_receipt(tmp_path) -> None:
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
        key = "523e4567-e89b-42d3-a456-426614174004"
        intake_id = intake_id_for_key(key)
        repository = app.state.application_repository
        repository.create_intake(
            InboxIntake(
                intake_id=intake_id,
                kind=IntakeKind.FILE,
                original_name="resume.bin",
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
                "error_code": "interrupted_before_asset_acceptance",
                "error_message": "upload interrupted before asset acceptance",
            },
        )
        assert failed.content_sha256 is None
        assert failed.asset_id is None

        resumed = client.post(
            "/api/v1/inbox/files",
            headers={**auth, "Idempotency-Key": key},
            files={"file": ("resume.bin", b"resumed bytes", "application/octet-stream")},
        )
        assert resumed.status_code == 201
        payload = resumed.json()
        assert payload["intake_id"] == intake_id
        assert payload["state"] == "partial"
        assert payload["content_sha256"] is not None
        assert payload["size_bytes"] == len(b"resumed bytes")
        assert payload["error_code"] == "media_probe_failed"
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
    child = create_app(root=tmp_path, max_upload_bytes=1234)
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
        config = client.get("/content-forge/app/config.js", headers=LOOPBACK_HEADERS)
        assert config.status_code == 200
        assert _pwa_config(config)["maxUploadBytes"] == 1234

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
