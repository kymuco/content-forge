from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application.production_presets import (
    ProductionPresetConflictError,
    ProductionPresetService,
)
from content_forge.core import AssetRef, Project, ProjectState
from content_forge.storage.records import SourceInput

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
            "label": "pr32-phone",
        },
    )
    assert exchanged.status_code == 200
    return exchanged.json()["token"]


def _inbox_image(library, tmp_path, name: str, payload: bytes) -> Project:
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
        metadata={"inbox_intake_id": f"cf_intake_{name.replace('.', '_')}"},
    )
    return library.save_project(project)


def test_pr32_preset_catalog_is_human_facing_and_bounded(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        token = _paired_token(client)
        response = client.get(
            "/api/v1/production/presets",
            headers={**LOOPBACK_HEADERS, "Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["preset_id"] for item in items] == [
            "hook_short",
            "top_bar_short",
            "framed_clip",
            "art_story",
            "panel_story",
        ]
        assert [item["label"] for item in items] == [
            "Hook Short",
            "Top Bar Short",
            "Framed Clip",
            "Art Story",
            "Panel Story",
        ]
        assert next(item for item in items if item["preset_id"] == "art_story")["image_only"] is True
        assert next(item for item in items if item["preset_id"] == "panel_story")["max_sources"] == 64
    finally:
        app.state.runtime_lease.close()


def test_pr32_create_request_is_idempotent_and_conflicting_reuse_fails_closed(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        source = _inbox_image(app.state.library, tmp_path, "one.png", b"first-image")
        service = ProductionPresetService(app.state.library)
        request_id = str(uuid.uuid4())
        first = service.create_project(
            request_id=request_id,
            preset_id="framed_clip",
            source_project_ids=(source.project_id,),
        )
        replay = service.create_project(
            request_id=request_id,
            preset_id="framed_clip",
            source_project_ids=(source.project_id,),
        )
        assert replay == first
        with pytest.raises(ProductionPresetConflictError):
            service.create_project(
                request_id=request_id,
                preset_id="art_story",
                source_project_ids=(source.project_id,),
            )
    finally:
        app.state.runtime_lease.close()


def test_pr32_api_builds_framed_project_and_reuses_existing_review_render_authority(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        first = _inbox_image(app.state.library, tmp_path, "a.png", b"image-a")
        second = _inbox_image(app.state.library, tmp_path, "b.png", b"image-b")
        token = _paired_token(client)
        response = client.post(
            "/api/v1/production/projects",
            headers={**LOOPBACK_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "request_id": str(uuid.uuid4()),
                "preset_id": "framed_clip",
                "source_project_ids": [first.project_id, second.project_id],
            },
        )
        assert response.status_code == 201, response.text
        summary = response.json()
        assert summary["state"] == "needs_review"
        assert summary["production_preset_id"] == "framed_clip"
        assert summary["production_preset_label"] == "Framed Clip"
        assert summary["review_renderable"] is True
        task_types = [item["task_type"] for item in summary["tasks"]]
        assert task_types == ["metadata", "preview_approval"]

        project = app.state.review.get_project(summary["project_id"])
        assert [scene.media.asset_id for scene in project.scenes] == [
            first.source_refs[0].asset_id,
            second.source_refs[0].asset_id,
        ]
        plan = app.state.review._compile_plan(project, "shorts_preview_540x960")
        assert plan.template_id == "content_frame"
        assert len(plan.scenes) == 2
    finally:
        app.state.runtime_lease.close()


def test_pr32_hook_short_keeps_existing_hook_and_crop_review_contract(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        source = _inbox_image(app.state.library, tmp_path, "hook.png", b"hook-image")
        token = _paired_token(client)
        response = client.post(
            "/api/v1/production/projects",
            headers={**LOOPBACK_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "request_id": str(uuid.uuid4()),
                "preset_id": "hook_short",
                "source_project_ids": [source.project_id],
            },
        )
        assert response.status_code == 201, response.text
        task_types = [item["task_type"] for item in response.json()["tasks"]]
        assert task_types == [
            "hook",
            "crop_confirmation",
            "metadata",
            "preview_approval",
        ]
    finally:
        app.state.runtime_lease.close()
