from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application.review import ReviewConflictError
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
            "label": "pr35-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {**LOOPBACK_HEADERS, "Authorization": f"Bearer {exchanged.json()['token']}"}


def _inbox_image(library, tmp_path: Path, name: str, payload: bytes) -> Project:
    path = tmp_path / name
    path.write_bytes(payload)
    result = library.assets.ingest_file(
        path,
        source=SourceInput(source_url=f"https://example.invalid/{name}"),
        mime_type="image/png",
    )
    ref = AssetRef(
        asset_id=result.asset.asset_id,
        source_id=None if result.source_record is None else result.source_record.source_id,
    )
    project = Project(
        content_kind="unclassified",
        state=ProjectState.INBOX,
        source_refs=(ref,),
        source_records=(() if result.source_record is None else (result.source_record,)),
        metadata={
            "inbox_intake_id": f"cf_intake_{uuid.uuid4().hex}",
            "original_filename": name,
        },
    )
    return library.save_project(project)


def _production_project(app, source: Project, *, preset_id: str = "framed_clip") -> Project:
    project = app.state.production_presets.create_project(
        request_id=str(uuid.uuid4()),
        preset_id=preset_id,
        source_project_ids=(source.project_id,),
    )
    return app.state.review.bootstrap_project(project.project_id)


def _preview_queue_item(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "state": "needs_review",
        "review_initialized": True,
        "review_renderable": True,
        "open_blocking_tasks": 1,
        "preview": {"status": "not_rendered"},
        "final": None,
        "tasks": [
            {
                "review_task_id": f"task-{project_id}",
                "task_type": "preview_approval",
                "attention": "review",
                "blocking": True,
                "status": "open",
                "payload": {"status": "not_rendered"},
                "suggestions": [],
                "accepted_value": None,
                "resolved_at": None,
            }
        ],
    }


def _publish_request(project_id: str, render_job_id: str, output_sha256: str) -> PublishRequest:
    return PublishRequest(
        contract_version="pr29_publish_contract_v2",
        artifact=PublishArtifactRef(
            project_id=project_id,
            render_job_id=render_job_id,
            profile_id="youtube_shorts_1080p",
            render_plan_digest="1" * 64,
            output_sha256=output_sha256,
            bytes_written=123,
            width=1080,
            height=1920,
            duration_seconds=8.0,
            has_audio=True,
        ),
        target=PublishTarget(provider_id="fixture", destination_id="channel-main"),
        metadata=PublishMetadata(title="PR35 fixture", visibility="private"),
        declarations=PublishDeclarations(
            child_directed=False,
            contains_realistic_altered_or_synthetic_media=False,
        ),
    )


def test_pr35_attention_is_authenticated_and_unused_source_leaves_inbox_after_use(
    tmp_path: Path,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        source = _inbox_image(app.state.library, tmp_path, "source.png", b"source-image")
        assert client.get("/api/v1/production/attention").status_code == 401
        headers = _paired_headers(client)

        initial = client.get("/api/v1/production/attention", headers=headers)
        assert initial.status_code == 200, initial.text
        source_cards = [item for item in initial.json()["items"] if item["kind"] == "source"]
        assert [item["source"]["source_project_id"] for item in source_cards] == [source.project_id]
        assert source_cards[0]["group"] == "inbox"

        production = _production_project(app, source)
        after = client.get("/api/v1/production/attention", headers=headers)
        assert after.status_code == 200, after.text
        assert all(
            item.get("source", {}).get("source_project_id") != source.project_id
            for item in after.json()["items"]
        )
        project_card = next(
            item
            for item in after.json()["items"]
            if item["kind"] == "project" and item["project"]["project_id"] == production.project_id
        )
        assert project_card["group"] == "safe_work"
        assert project_card["safe_operation"] == "render_preview"

        # Leaving the attention Inbox is not deletion/consumption authority: reusable
        # source media remains available in the Create video catalog.
        sources = client.get("/api/v1/production/sources", headers=headers)
        assert sources.status_code == 200
        assert source.project_id in {
            item["source_project_id"] for item in sources.json()["items"]
        }
    finally:
        app.state.runtime_lease.close()


def test_pr35_safe_work_excludes_raw_source_even_if_legacy_review_queue_lists_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        source = _inbox_image(app.state.library, tmp_path, "raw.png", b"raw-image")
        production = _production_project(app, source)
        real_queue = app.state.review.list_queue
        real_payload = real_queue(limit=500, include_auto=False)
        production_item = next(
            item for item in real_payload["items"] if item["project_id"] == production.project_id
        )

        def fake_queue(*, limit: int = 100, include_auto: bool = False):
            del limit, include_auto
            return {
                "items": [_preview_queue_item(source.project_id), production_item],
                "ready_projects": [source.project_id],
            }

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(app.state.review, "list_queue", fake_queue)
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

        headers = _paired_headers(client)
        response = client.post(
            "/api/v1/production/safe-work",
            headers=headers,
            json={"render_limit": 4},
        )
        assert response.status_code == 200, response.text
        assert calls == [("preview", production.project_id)]
        assert all(project_id != source.project_id for _, project_id in calls)
    finally:
        app.state.runtime_lease.close()


def test_pr35_safe_work_prioritizes_final_then_preview_and_honors_render_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        final_a = new_entity_id(EntityKind.PROJECT)
        final_b = new_entity_id(EntityKind.PROJECT)
        preview = new_entity_id(EntityKind.PROJECT)

        def fake_queue(*, limit: int = 100, include_auto: bool = False):
            del limit, include_auto
            return {
                "items": [_preview_queue_item(preview)],
                "ready_projects": [final_b, final_a],
            }

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(app.state.review, "list_queue", fake_queue)
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

        headers = _paired_headers(client)
        response = client.post(
            "/api/v1/production/safe-work",
            headers=headers,
            json={"render_limit": 2},
        )
        assert response.status_code == 200, response.text
        assert calls == [("final", final_a), ("final", final_b)]
        payload = response.json()
        assert payload["render_candidates"] == 3
        assert payload["rendered"] == 2
        assert payload["remaining_render_candidates"] == 1
    finally:
        app.state.runtime_lease.close()


def test_pr35_safe_work_quarantines_review_failure_and_continues_other_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        first = new_entity_id(EntityKind.PROJECT)
        second = new_entity_id(EntityKind.PROJECT)

        def fake_queue(*, limit: int = 100, include_auto: bool = False):
            del limit, include_auto
            return {"items": [], "ready_projects": [first, second]}

        calls: list[str] = []

        def render_final(project_id: str):
            calls.append(project_id)
            if project_id == first:
                raise ReviewConflictError("synthetic safe failure")
            return {}

        monkeypatch.setattr(app.state.review, "list_queue", fake_queue)
        monkeypatch.setattr(app.state.review, "render_final", render_final)
        headers = _paired_headers(client)
        response = client.post(
            "/api/v1/production/safe-work",
            headers=headers,
            json={"render_limit": 2},
        )
        assert response.status_code == 200, response.text
        assert calls == sorted([first, second])
        results = response.json()["results"]
        assert [item["outcome"] for item in results] == ["failed", "succeeded"]
        assert results[0]["error_code"] == "ReviewConflictError"
        assert "synthetic safe failure" not in response.text
    finally:
        app.state.runtime_lease.close()


def test_pr35_attention_preserves_retry_blocking_unknown_publish_outcome(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        source = _inbox_image(app.state.library, tmp_path, "publish.png", b"publish-image")
        production = _production_project(app, source)
        render_job_id = new_entity_id(EntityKind.JOB)
        output_sha256 = "a" * 64
        metadata = dict(production.metadata)
        metadata.update(
            {
                "final_render_job_id": render_job_id,
                "final_render_plan_digest": "1" * 64,
                "final_output_sha256": output_sha256,
            }
        )
        done = production.validated_copy(
            update={"state": ProjectState.DONE, "metadata": metadata}
        )
        app.state.library.save_project(done)

        request = _publish_request(done.project_id, render_job_id, output_sha256)
        attempt = app.state.library.publishing.prepare_attempt(approve_publish_request(request))
        repository = app.state.library.publishing
        repository.mark_running(
            attempt.attempt_id,
            PublishingProviderHealth(
                provider_id="fixture",
                provider_version="fixture-v1",
                available=True,
            ),
        )
        repository.mark_outcome_unknown(
            attempt.attempt_id,
            code="synthetic_unknown",
            message="synthetic remote outcome is unknown",
        )

        headers = _paired_headers(client)
        response = client.get("/api/v1/production/attention", headers=headers)
        assert response.status_code == 200, response.text
        card = next(
            item
            for item in response.json()["items"]
            if item["kind"] == "project" and item["project"]["project_id"] == done.project_id
        )
        assert card["group"] == "failed"
        assert card["publish_state"] == "outcome_unknown"
        assert "unknown" in card["reason"].lower()
    finally:
        app.state.runtime_lease.close()
