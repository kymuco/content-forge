from __future__ import annotations

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
            "label": "pr26-hardening",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def _oversized_exact_tags() -> list[dict[str, str]]:
    return [{"kind": "topic", "value": f"topic-{index}"} for index in range(129)]


def test_pr26_api_bounds_exact_tag_query_complexity(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        search = client.post(
            "/api/v1/production-library/search",
            headers=headers,
            json={"tags": _oversized_exact_tags()},
        )
        assert search.status_code == 422
        assert "exceeds 128 exact tags" in search.json()["detail"]

        collection = client.put(
            "/api/v1/production-library/collections/too_many_tags",
            headers=headers,
            json={
                "name": "Too many tags",
                "query": {"tags": _oversized_exact_tags()},
            },
        )
        assert collection.status_code == 422
        assert "exceeds 128 exact tags" in collection.json()["detail"]
    finally:
        app.state.runtime_lease.close()


def test_pr26_api_requires_exact_application_json_media_type(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        response = client.post(
            "/api/v1/production-library/search",
            headers={**headers, "Content-Type": "application/json-patch+json"},
            content=b"{}",
        )
        assert response.status_code == 415
        assert response.json()["detail"] == "application/json is required"
    finally:
        app.state.runtime_lease.close()


def test_pr26_api_future_schema_failure_is_controlled_for_all_routes(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        with app.state.library.database.transaction() as connection:
            connection.execute(
                "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                ("production_library", 2),
            )

        response = client.get(
            "/api/v1/production-library/collections",
            headers=headers,
        )
        assert response.status_code == 500
        assert response.json() == {"detail": "production library schema unavailable"}
    finally:
        app.state.runtime_lease.close()
