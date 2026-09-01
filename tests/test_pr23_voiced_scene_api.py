from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import ProjectVoicedSceneManifest, ProjectVoicedScenePlan
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
            "label": "pr23-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def test_pr23_auth_precedes_json_parsing(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    try:
        response = client.post(
            f"/api/v1/voiced-scene/projects/{project_id}/materialize",
            headers={"Content-Type": "application/json"},
            content=b"{not-json",
        )
        assert response.status_code == 401
    finally:
        client.close()


def test_pr23_preview_and_materialize_unknown_project_are_404(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    try:
        headers = _paired_headers(client)
        preview = client.get(
            f"/api/v1/voiced-scene/projects/{project_id}/preview",
            headers=headers,
        )
        assert preview.status_code == 404

        materialize = client.post(
            f"/api/v1/voiced-scene/projects/{project_id}/materialize",
            headers=headers,
            json={},
        )
        assert materialize.status_code == 404
    finally:
        client.close()


def test_pr23_transport_rejects_wrong_content_type_and_large_body(tmp_path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    try:
        auth = _paired_headers(client)
        wrong_type = client.post(
            f"/api/v1/voiced-scene/projects/{project_id}/materialize",
            headers={**auth, "Content-Type": "text/plain"},
            content=b"{}",
        )
        assert wrong_type.status_code == 415

        too_large = client.post(
            f"/api/v1/voiced-scene/projects/{project_id}/materialize",
            headers={**auth, "Content-Type": "application/json"},
            content=b" " * (64 * 1024 + 1),
        )
        assert too_large.status_code == 413
    finally:
        client.close()


def test_pr23_installed_surface_returns_preview_manifest_and_dematerializes(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    plan = ProjectVoicedScenePlan(
        project_id=project_id,
        pr22_manifest_sha256="a" * 64,
    )
    manifest = ProjectVoicedSceneManifest(
        project_id=project_id,
        plan=plan,
    )
    workflow = app.state.voiced_scene
    monkeypatch.setattr(workflow, "preview", lambda value, preset=None: plan)
    monkeypatch.setattr(workflow, "manifest", lambda value: manifest)
    monkeypatch.setattr(workflow, "materialize", lambda value, preset=None: manifest)
    monkeypatch.setattr(workflow, "dematerialize", lambda value: True)
    try:
        headers = _paired_headers(client)
        preview = client.get(
            f"/api/v1/voiced-scene/projects/{project_id}/preview",
            headers=headers,
        )
        assert preview.status_code == 200
        assert preview.json()["contract_version"] == "pr23_voiced_scene_plan_v1"
        assert preview.json()["project_id"] == project_id

        current = client.get(
            f"/api/v1/voiced-scene/projects/{project_id}",
            headers=headers,
        )
        assert current.status_code == 200
        assert current.json()["contract_version"] == "pr23_voiced_scene_manifest_v1"
        assert current.json()["plan"]["project_id"] == project_id

        materialized = client.post(
            f"/api/v1/voiced-scene/projects/{project_id}/materialize",
            headers=headers,
            json={},
        )
        assert materialized.status_code == 200
        payload = materialized.json()
        assert payload["contract_version"] == "pr23_voiced_scene_manifest_v1"
        assert payload["project_id"] == project_id
        assert payload["plan"]["contract_version"] == "pr23_voiced_scene_plan_v1"
        assert payload["plan"]["issues"] == []

        removed = client.delete(
            f"/api/v1/voiced-scene/projects/{project_id}/materialization",
            headers=headers,
        )
        assert removed.status_code == 204
        assert removed.content == b""
    finally:
        client.close()
