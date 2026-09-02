from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from fastapi.testclient import TestClient

from content_forge.api import create_app


LOOPBACK_HEADERS = {"Host": "localhost"}


def _paired_headers(client: TestClient) -> dict[str, str]:
    challenge = client.post("/api/v1/pairing/challenges", headers=LOOPBACK_HEADERS)
    assert challenge.status_code == 201
    payload = challenge.json()
    exchanged = client.post(
        "/api/v1/pairing/exchange",
        headers=LOOPBACK_HEADERS,
        json={
            "challenge_id": payload["challenge_id"],
            "code": payload["code"],
            "label": "pr27-review-regression",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def test_pr27_initial_restart_reconciliation_is_serialized(tmp_path) -> None:
    app = create_app(root=tmp_path)
    first_client = TestClient(app)
    second_client = TestClient(app)
    try:
        headers = _paired_headers(first_client)
        first_reconcile_entered = Event()
        second_request_started = Event()
        second_reconcile_entered = Event()
        release_first_reconcile = Event()
        count_lock = Lock()
        reconcile_calls = 0

        def blocked_reconcile() -> int:
            nonlocal reconcile_calls
            with count_lock:
                reconcile_calls += 1
                call_number = reconcile_calls
            if call_number == 1:
                first_reconcile_entered.set()
                assert release_first_reconcile.wait(timeout=5.0)
            else:
                second_reconcile_entered.set()
            return 0

        app.state.publishing.reconcile_interrupted = blocked_reconcile

        def first_status():
            return first_client.get("/api/v1/publishing/status", headers=headers)

        def second_status():
            second_request_started.set()
            return second_client.get("/api/v1/publishing/status", headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_status)
            assert first_reconcile_entered.wait(timeout=5.0)
            second = pool.submit(second_status)
            assert second_request_started.wait(timeout=5.0)

            # A second initial request must wait behind the one-time reconciliation
            # gate rather than starting another stale reconciliation concurrently.
            assert not second_reconcile_entered.wait(timeout=1.0)
            release_first_reconcile.set()

            assert first.result(timeout=5.0).status_code == 200
            assert second.result(timeout=5.0).status_code == 200

        assert reconcile_calls == 1
    finally:
        app.state.runtime_lease.close()


def test_pr27_pwa_execution_is_bound_to_the_loaded_attempt_id(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        script = client.get("/app/publishing.js")
        assert script.status_code == 200
        assert "let loadedAttemptId = null;" in script.text
        assert 'attemptInput.addEventListener("input", invalidateLoadedAttempt);' in script.text
        assert 'attemptInput.addEventListener("change", invalidateLoadedAttempt);' in script.text
        assert "const attemptId = loadedAttemptId;" in script.text
        assert "attemptInput.value.trim() !== attemptId" in script.text
        assert "Load the exact publish attempt before execution." in script.text
    finally:
        app.state.runtime_lease.close()
