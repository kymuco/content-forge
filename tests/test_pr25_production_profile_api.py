from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import Project, ProjectState
from content_forge.profiles import long_form_1080p_profile, shorts_final_profile
from content_forge.templates import (
    CONTENT_FRAME_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    INITIAL_TEMPLATE_VERSION,
)

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
            "label": "pr25-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def _profile_payload(profile_id: str, *, horizontal: bool = False) -> dict[str, object]:
    template_id = CONTENT_FRAME_TEMPLATE_ID if horizontal else HOOK_OVERLAY_TEMPLATE_ID
    template_version = INITIAL_TEMPLATE_VERSION if horizontal else HOOK_OVERLAY_TEMPLATE_VERSION
    output = long_form_1080p_profile() if horizontal else shorts_final_profile()
    return {
        "profile_id": profile_id,
        "scope": "channel",
        "display_name": profile_id,
        "default_template": {
            "template_id": template_id,
            "version": template_version,
        },
        "default_languages": ["en"],
        "output_profiles": [output.model_dump(mode="json")],
        "branding": {"display_name": profile_id},
    }


def test_pr25_profile_http_registry_bind_rebind_and_unbind(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        project = app.state.library.save_project(
            Project(content_kind="profile_api_fixture", state=ProjectState.DRAFT)
        )

        created_a = client.post(
            "/api/v1/production-profiles",
            headers=headers,
            json=_profile_payload("channel_a"),
        )
        assert created_a.status_code == 201
        revision_a = created_a.json()
        assert revision_a["revision"] == 1

        idempotent = client.post(
            "/api/v1/production-profiles",
            headers=headers,
            json=_profile_payload("channel_a"),
        )
        assert idempotent.status_code == 200
        assert idempotent.json() == revision_a

        created_b = client.post(
            "/api/v1/production-profiles",
            headers=headers,
            json=_profile_payload("channel_b", horizontal=True),
        )
        assert created_b.status_code == 201

        listed = client.get("/api/v1/production-profiles", headers=headers)
        assert listed.status_code == 200
        assert [item["profile_id"] for item in listed.json()["items"]] == [
            "channel_a",
            "channel_b",
        ]

        exact = client.get(
            "/api/v1/production-profiles/registry/channel_a?revision=1",
            headers=headers,
        )
        assert exact.status_code == 200
        assert exact.json() == revision_a

        initial = client.get(
            f"/api/v1/production-profiles/projects/{project.project_id}",
            headers=headers,
        )
        assert initial.status_code == 200
        assert initial.json()["profile"] is None

        bound = client.put(
            f"/api/v1/production-profiles/projects/{project.project_id}",
            headers=headers,
            json={"profile_id": "channel_a", "revision": 1},
        )
        assert bound.status_code == 200
        assert bound.json()["profile"]["revision"]["profile_id"] == "channel_a"
        assert bound.json()["template"]["template_id"] == HOOK_OVERLAY_TEMPLATE_ID

        rebound = client.put(
            f"/api/v1/production-profiles/projects/{project.project_id}",
            headers=headers,
            json={"profile_id": "channel_b", "revision": 1},
        )
        assert rebound.status_code == 200
        assert rebound.json()["profile"]["revision"]["profile_id"] == "channel_b"
        assert rebound.json()["template"]["template_id"] == CONTENT_FRAME_TEMPLATE_ID
        assert rebound.json()["output_profiles"][0]["profile_id"] == "long_form_1080p"

        removed = client.delete(
            f"/api/v1/production-profiles/projects/{project.project_id}",
            headers=headers,
        )
        assert removed.status_code == 200
        assert removed.json()["profile"] is None
        assert removed.json()["template"] is None
        assert removed.json()["output_profiles"] == []
    finally:
        app.state.runtime_lease.close()


def test_pr25_profile_transport_auth_precedes_json_validation(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        unauthenticated = client.post(
            "/api/v1/production-profiles",
            headers=LOOPBACK_HEADERS,
            content=b"{not-json",
        )
        assert unauthenticated.status_code == 401

        headers = _paired_headers(client)
        oversized = client.post(
            "/api/v1/production-profiles",
            headers={**headers, "Content-Type": "application/json", "Content-Length": str(300 * 1024)},
            content=b"{}",
        )
        assert oversized.status_code == 413
    finally:
        app.state.runtime_lease.close()


def test_pr25_project_profile_routes_reject_malformed_project_ids_as_422(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        malformed = "not-a-project-id"

        loaded = client.get(
            f"/api/v1/production-profiles/projects/{malformed}",
            headers=headers,
        )
        assert loaded.status_code == 422
        assert loaded.json()["detail"] == "invalid project ID"

        rebound = client.put(
            f"/api/v1/production-profiles/projects/{malformed}",
            headers=headers,
            json={"profile_id": "channel_a", "revision": 1},
        )
        assert rebound.status_code == 422
        assert rebound.json()["detail"] == "invalid project ID"

        removed = client.delete(
            f"/api/v1/production-profiles/projects/{malformed}",
            headers=headers,
        )
        assert removed.status_code == 422
        assert removed.json()["detail"] == "invalid project ID"
    finally:
        app.state.runtime_lease.close()
