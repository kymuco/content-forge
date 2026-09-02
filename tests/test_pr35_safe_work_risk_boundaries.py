from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import AssetRef, EntityKind, Project, ProjectState, new_entity_id
from content_forge.providers import (
    PublishArtifactRef,
    PublishDeclarations,
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    PublishingProviderHealth,
    approve_publish_request,
)
from content_forge.storage.records import SourceInput

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
            "label": "pr35-safe-boundaries",
        },
    )
    assert exchanged.status_code == 200
    return {**LOOPBACK_HEADERS, "Authorization": f"Bearer {exchanged.json()['token']}"}


def _inbox_image(library, tmp_path: Path, name: str) -> Project:
    path = tmp_path / name
    path.write_bytes(b"old-source")
    result = library.assets.ingest_file(
        path,
        source=SourceInput(source_url=f"https://example.invalid/{name}"),
        mime_type="image/png",
    )
    ref = AssetRef(
        asset_id=result.asset.asset_id,
        source_id=None if result.source_record is None else result.source_record.source_id,
    )
    return library.save_project(
        Project(
            content_kind="unclassified",
            state=ProjectState.INBOX,
            source_refs=(ref,),
            source_records=(() if result.source_record is None else (result.source_record,)),
            metadata={
                "inbox_intake_id": f"cf_intake_{uuid.uuid4().hex}",
                "original_filename": name,
            },
        )
    )


def _queue_task(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "project_state": "needs_review",
        "content_kind": "unclassified",
        "task": {
            "review_task_id": f"task-{project_id}",
            "task_type": "preview_approval",
            "attention": "review",
            "priority": "blocking",
            "blocking": True,
            "status": "open",
            "payload": {"status": "not_rendered"},
            "suggestions": [],
            "accepted_value": None,
            "resolved_at": None,
        },
    }


def _ready_summary(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "state": "ready",
        "content_kind": "unclassified",
        "review_initialized": True,
        "review_renderable": True,
        "open_blocking_tasks": 0,
        "preview": {"status": "ready"},
        "final": None,
        "tasks": [],
    }


def _publish_request(project_id: str) -> PublishRequest:
    return PublishRequest(
        contract_version="pr29_publish_contract_v2",
        artifact=PublishArtifactRef(
            project_id=project_id,
            render_job_id=new_entity_id(EntityKind.JOB),
            profile_id="youtube_shorts_1080p",
            render_plan_digest="d" * 64,
            output_sha256="e" * 64,
            bytes_written=123,
            width=1080,
            height=1920,
            duration_seconds=8.0,
            has_audio=True,
        ),
        target=PublishTarget(provider_id="fixture", destination_id="channel-main"),
        metadata=PublishMetadata(title="Remote risk", visibility="private"),
        declarations=PublishDeclarations(
            child_directed=False,
            contains_realistic_altered_or_synthetic_media=False,
        ),
    )


def test_pr35_old_raw_source_is_excluded_even_when_bounded_source_catalog_misses_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        source = _inbox_image(app.state.library, tmp_path, "old.png")
        monkeypatch.setattr(app.state.production_presets, "list_sources", lambda *, limit: ())
        monkeypatch.setattr(
            app.state.review,
            "list_queue",
            lambda *, limit=100, include_auto=False: {
                "items": [_queue_task(source.project_id)],
                "ready_projects": [_ready_summary(source.project_id)],
            },
        )

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app.state.review,
            "render_preview",
            lambda project_id: calls.append(("preview", project_id)) or {},
        )
        monkeypatch.setattr(
            app.state.review,
            "render_final",
            lambda project_id: calls.append(("final", project_id)) or {},
        )

        response = client.post(
            "/api/v1/production/safe-work",
            headers=_paired_headers(client),
            json={"render_limit": 4},
        )
        assert response.status_code == 200, response.text
        assert calls == []
        assert response.json()["render_candidates"] == 0
    finally:
        app.state.runtime_lease.close()


def test_pr35_active_remote_risk_blocks_automatic_local_render_for_same_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        project = app.state.library.save_project(
            Project(content_kind="unclassified", state=ProjectState.INBOX)
        )
        request = _publish_request(project.project_id)
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

        monkeypatch.setattr(
            app.state.review,
            "list_queue",
            lambda *, limit=100, include_auto=False: {
                "items": [_queue_task(project.project_id)],
                "ready_projects": [_ready_summary(project.project_id)],
            },
        )

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app.state.review,
            "render_preview",
            lambda project_id: calls.append(("preview", project_id)) or {},
        )
        monkeypatch.setattr(
            app.state.review,
            "render_final",
            lambda project_id: calls.append(("final", project_id)) or {},
        )

        response = client.post(
            "/api/v1/production/safe-work",
            headers=_paired_headers(client),
            json={"render_limit": 4},
        )
        assert response.status_code == 200, response.text
        assert calls == []
        payload = response.json()
        assert payload["render_candidates"] == 0
        card = next(
            item
            for item in payload["attention"]["items"]
            if item.get("project", {}).get("project_id") == project.project_id
        )
        assert card["group"] == "failed"
        assert card["publish_state"] == "outcome_unknown"
    finally:
        app.state.runtime_lease.close()
