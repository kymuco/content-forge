from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application.idempotency import intake_id_for_key
from content_forge.web import static_path

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
            "label": "pr9-final-regression",
        },
    )
    assert exchanged.status_code == 200
    return exchanged.json()["token"]


def test_auth_cleanup_is_scoped_to_the_bearer_that_sent_the_request() -> None:
    client = static_path("app.js").read_text(encoding="utf-8")
    shared = static_path("shared.js").read_text(encoding="utf-8")

    assert "async function apiFetchWithBearer" in client
    assert "const requestBearer = bearerToken" in client
    assert "return { response, requestBearer }" in client
    assert "error.requestBearer = requestBearer" in client
    assert "async function finalizeInvalidatedSession(message, kind, expectedBearer)" in client
    assert "bearerToken !== invalidatedBearer" in client
    assert "clearTokenIfMatches(invalidatedBearer)" in client
    assert "error.requestBearer !== bearerToken" in client
    assert "requestBearer !== bearerToken" in client
    assert "redrainForNewBearer" in client

    assert "async function clearTokenIfMatches(expectedToken)" in shared
    assert "if (read.result !== expectedToken) return" in shared
    assert "store.delete(TOKEN_KEY)" in shared
    assert "clearTokenIfMatches," in shared


def test_pairing_persistence_failure_exposes_disconnect_before_auto_cleanup() -> None:
    client = static_path("app.js").read_text(encoding="utf-8")

    start = client.index("async function exchangePairing")
    end = client.index("async function handlePairForm")
    pairing = client[start:end]

    catch_start = pairing.index("catch (storageError)")
    persistence_failure = pairing[catch_start:]
    assert "bearerToken = issuedToken" in persistence_failure
    assert "setPairedState(true)" in persistence_failure
    assert "Disconnect remains available while cleanup is pending" in persistence_failure
    auto_cleanup = "await revokeIssuedPairingToken(issuedToken)"
    assert auto_cleanup in persistence_failure
    assert persistence_failure.index("bearerToken = issuedToken") < persistence_failure.index(auto_cleanup)
    assert persistence_failure.index("setPairedState(true)") < persistence_failure.index(auto_cleanup)


def test_pairing_token_persistence_is_cross_tab_compare_and_set() -> None:
    client = static_path("app.js").read_text(encoding="utf-8")
    shared = static_path("shared.js").read_text(encoding="utf-8")

    start = shared.index("async function setToken(token)")
    end = shared.index("async function clearToken()")
    claim = shared[start:end]

    assert 'db.transaction(KV_STORE, "readwrite")' in claim
    assert "const read = store.get(TOKEN_KEY)" in claim
    assert "const existing = read.result" in claim
    assert "if (existing !== undefined)" in claim
    assert "if (existing === token) return" in claim
    assert 'new Error("pairing token slot is already occupied")' in claim
    assert "tx.abort()" in claim
    assert "const write = store.put(token, TOKEN_KEY)" in claim
    assert claim.index("const read = store.get(TOKEN_KEY)") < claim.index("const write = store.put(token, TOKEN_KEY)")
    assert claim.index("if (existing !== undefined)") < claim.index("const write = store.put(token, TOKEN_KEY)")

    pairing_start = client.index("async function exchangePairing")
    pairing_end = client.index("async function handlePairForm")
    pairing = client[pairing_start:pairing_end]
    assert "await window.CFStore.setToken(issuedToken)" in pairing
    catch_start = pairing.index("catch (storageError)")
    loser = pairing[catch_start:]
    assert "bearerToken = issuedToken" in loser
    assert "await revokeIssuedPairingToken(issuedToken)" in loser
    assert "finalizeInvalidatedSession" in loser


def test_oversized_idempotent_receipt_retries_after_limit_increase(tmp_path) -> None:
    key = "623e4567-e89b-42d3-a456-426614174005"
    payload = b"x" * 65

    small = create_app(
        root=tmp_path,
        ffprobe_path="definitely-missing-ffprobe",
        ffmpeg_path="definitely-missing-ffmpeg",
        max_upload_bytes=64,
    )
    client = TestClient(small)
    token = _paired_token(client)
    auth = {
        **LOOPBACK_HEADERS,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }
    try:
        rejected = client.post(
            "/api/v1/inbox/files",
            headers=auth,
            files={"file": ("limit-change.bin", payload, "application/octet-stream")},
        )
        assert rejected.status_code == 413
        receipt = small.state.application_repository.get_intake(intake_id_for_key(key))
        assert receipt is not None
        assert receipt.state.value == "failed"
        assert receipt.content_sha256 is None
        assert receipt.error_code == "UploadTooLargeError"
    finally:
        small.state.runtime_lease.close()

    larger = create_app(
        root=tmp_path,
        ffprobe_path="definitely-missing-ffprobe",
        ffmpeg_path="definitely-missing-ffmpeg",
        max_upload_bytes=128,
    )
    retry_client = TestClient(larger)
    try:
        accepted = retry_client.post(
            "/api/v1/inbox/files",
            headers=auth,
            files={"file": ("limit-change.bin", payload, "application/octet-stream")},
        )
        assert accepted.status_code == 201
        body = accepted.json()
        assert body["intake_id"] == intake_id_for_key(key)
        assert body["size_bytes"] == len(payload)
        assert body["content_sha256"] is not None
    finally:
        larger.state.runtime_lease.close()


def test_accepted_idempotent_replay_survives_later_limit_decrease(tmp_path) -> None:
    key = "723e4567-e89b-42d3-a456-426614174006"
    unknown_key = "823e4567-e89b-42d3-a456-426614174007"
    payload = b"a" * (1024 * 1024 + 128)

    larger = create_app(
        root=tmp_path,
        ffprobe_path="definitely-missing-ffprobe",
        ffmpeg_path="definitely-missing-ffmpeg",
        max_upload_bytes=len(payload),
    )
    client = TestClient(larger)
    token = _paired_token(client)
    auth = {
        **LOOPBACK_HEADERS,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }
    try:
        first = client.post(
            "/api/v1/inbox/files",
            headers=auth,
            files={"file": ("accepted-large.bin", payload, "application/octet-stream")},
        )
        assert first.status_code == 201
        first_body = first.json()
        assert first_body["intake_id"] == intake_id_for_key(key)
        assert first_body["size_bytes"] == len(payload)
        assert first_body["content_sha256"] is not None
        first_project_id = first_body["project_id"]
    finally:
        larger.state.runtime_lease.close()

    smaller = create_app(
        root=tmp_path,
        ffprobe_path="definitely-missing-ffprobe",
        ffmpeg_path="definitely-missing-ffmpeg",
        max_upload_bytes=64,
    )
    retry_client = TestClient(smaller)
    try:
        # An unrelated key receives only the current runtime's normal body allowance.
        unknown = retry_client.post(
            "/api/v1/inbox/files",
            headers={
                **LOOPBACK_HEADERS,
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": unknown_key,
            },
            files={"file": ("accepted-large.bin", payload, "application/octet-stream")},
        )
        assert unknown.status_code == 413

        replay = retry_client.post(
            "/api/v1/inbox/files",
            headers=auth,
            files={"file": ("accepted-large.bin", payload, "application/octet-stream")},
        )
        assert replay.status_code == 201
        replay_body = replay.json()
        assert replay_body["intake_id"] == first_body["intake_id"]
        assert replay_body["project_id"] == first_project_id
        assert replay_body["size_bytes"] == len(payload)
        assert replay_body["content_sha256"] == first_body["content_sha256"]

        conflict = retry_client.post(
            "/api/v1/inbox/files",
            headers=auth,
            files={
                "file": (
                    "accepted-large.bin",
                    b"b" * len(payload),
                    "application/octet-stream",
                )
            },
        )
        assert conflict.status_code == 409
        assert "different file bytes" in conflict.json()["detail"]

        assert len(smaller.state.inbox.list_intakes()) == 1
    finally:
        smaller.state.runtime_lease.close()
