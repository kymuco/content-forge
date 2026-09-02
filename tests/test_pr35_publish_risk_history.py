from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import EntityKind, Project, ProjectState, new_entity_id
from content_forge.providers import (
    PublishArtifactRef,
    PublishDeclarations,
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    PublishingProviderHealth,
    approve_publish_request,
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
            "label": "pr35-history",
        },
    )
    assert exchanged.status_code == 200
    return {**LOOPBACK_HEADERS, "Authorization": f"Bearer {exchanged.json()['token']}"}


def _publish_request(project_id: str, *, marker: str) -> PublishRequest:
    return PublishRequest(
        contract_version="pr29_publish_contract_v2",
        artifact=PublishArtifactRef(
            project_id=project_id,
            render_job_id=new_entity_id(EntityKind.JOB),
            profile_id="youtube_shorts_1080p",
            render_plan_digest=marker * 64,
            output_sha256=marker * 64,
            bytes_written=123,
            width=1080,
            height=1920,
            duration_seconds=8.0,
            has_audio=True,
        ),
        target=PublishTarget(provider_id="fixture", destination_id="channel-main"),
        metadata=PublishMetadata(title=f"History {marker}", visibility="private"),
        declarations=PublishDeclarations(
            child_directed=False,
            contains_realistic_altered_or_synthetic_media=False,
        ),
    )


def test_pr35_outcome_unknown_survives_recent_project_and_review_windows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        project = app.state.library.save_project(
            Project(content_kind="unclassified", state=ProjectState.INBOX)
        )
        request = _publish_request(project.project_id, marker="a")
        attempt = app.state.library.publishing.prepare_attempt(approve_publish_request(request))
        app.state.library.publishing.mark_running(
            attempt.attempt_id,
            PublishingProviderHealth(
                provider_id="fixture",
                provider_version="fixture-v1",
                available=True,
            ),
        )
        app.state.library.publishing.mark_outcome_unknown(
            attempt.attempt_id,
            code="synthetic_unknown",
            message="synthetic remote outcome is unknown",
        )

        # Simulate a project older than every ordinary bounded daily/review window. The
        # durable publish ledger must independently pull it back into the safety surface.
        monkeypatch.setattr(app.state.production_presets, "list_projects", lambda *, limit: ())
        monkeypatch.setattr(
            app.state.review,
            "list_queue",
            lambda *, limit=100, include_auto=False: {"items": [], "ready_projects": []},
        )

        response = client.get(
            "/api/v1/production/attention?limit=1",
            headers=_paired_headers(client),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["counts"]["failed"] == 1
        assert payload["items"] == [
            {
                "kind": "project",
                "group": "failed",
                "reason": "Remote publishing outcome is unknown for a stored final",
                "safe_operation": None,
                "publish_state": "outcome_unknown",
                "project": {
                    "project_id": project.project_id,
                    "state": "inbox",
                    "content_kind": "unclassified",
                    "review_initialized": False,
                    "review_renderable": False,
                    "open_blocking_tasks": 0,
                    "preview": None,
                    "final": None,
                    "tasks": [],
                },
            }
        ]
    finally:
        app.state.runtime_lease.close()


def test_pr35_stronger_unknown_remote_state_outranks_prepared_state_for_same_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        project = app.state.library.save_project(
            Project(content_kind="unclassified", state=ProjectState.INBOX)
        )
        prepared_request = _publish_request(project.project_id, marker="b")
        app.state.library.publishing.prepare_attempt(
            approve_publish_request(prepared_request)
        )

        unknown_request = _publish_request(project.project_id, marker="c")
        unknown = app.state.library.publishing.prepare_attempt(
            approve_publish_request(unknown_request)
        )
        app.state.library.publishing.mark_running(
            unknown.attempt_id,
            PublishingProviderHealth(
                provider_id="fixture",
                provider_version="fixture-v1",
                available=True,
            ),
        )
        app.state.library.publishing.mark_outcome_unknown(
            unknown.attempt_id,
            code="synthetic_unknown",
            message="synthetic remote outcome is unknown",
        )

        monkeypatch.setattr(app.state.production_presets, "list_projects", lambda *, limit: ())
        monkeypatch.setattr(
            app.state.review,
            "list_queue",
            lambda *, limit=100, include_auto=False: {"items": [], "ready_projects": []},
        )

        response = client.get(
            "/api/v1/production/attention",
            headers=_paired_headers(client),
        )
        assert response.status_code == 200, response.text
        card = next(
            item
            for item in response.json()["items"]
            if item.get("project", {}).get("project_id") == project.project_id
        )
        assert card["group"] == "failed"
        assert card["publish_state"] == "outcome_unknown"
        assert card["safe_operation"] is None
    finally:
        app.state.runtime_lease.close()
