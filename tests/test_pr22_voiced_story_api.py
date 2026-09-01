from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import Project

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
            "label": "pr22-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def test_pr22_auth_precedes_json_parsing(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    try:
        response = client.post(
            f"/api/v1/voiced-story/projects/{project_id}/materialize",
            headers={"Content-Type": "application/json"},
            content=b"{not-json",
        )
        assert response.status_code == 401
    finally:
        client.close()


def test_pr22_preview_and_materialize_unknown_project_are_404(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    try:
        headers = _paired_headers(client)
        preview = client.get(
            f"/api/v1/voiced-story/projects/{project_id}/preview",
            headers=headers,
        )
        assert preview.status_code == 404

        materialize = client.post(
            f"/api/v1/voiced-story/projects/{project_id}/materialize",
            headers=headers,
            json={},
        )
        assert materialize.status_code == 404
    finally:
        client.close()


def test_pr22_transport_rejects_wrong_content_type_and_large_body(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    try:
        auth = _paired_headers(client)
        wrong_type = client.post(
            f"/api/v1/voiced-story/projects/{project_id}/materialize",
            headers={**auth, "Content-Type": "text/plain"},
            content=b"{}",
        )
        assert wrong_type.status_code == 415

        too_large = client.post(
            f"/api/v1/voiced-story/projects/{project_id}/materialize",
            headers={**auth, "Content-Type": "application/json"},
            content=b" " * (64 * 1024 + 1),
        )
        assert too_large.status_code == 413
    finally:
        client.close()
