from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import ApplicationRepository, PanelOCRWorkflow
from content_forge.core import AssetRef, MediaType, Project, ProjectState, Scene
from content_forge.providers import (
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRRegion,
    OCRRequest,
    OCRResult,
    semantic_ocr_request_digest,
)

LOOPBACK_HEADERS = {"Host": "localhost"}


class _OCRProvider:
    def health(self):  # pragma: no cover
        raise AssertionError

    def extract(self, request: OCRRequest) -> OCRResult:
        return OCRResult(
            source_sha256=request.source_sha256,
            width=request.width,
            height=request.height,
            regions=(
                OCRRegion(
                    region_id="ocr_0000",
                    provider_index=0,
                    raw_text="Top bubble",
                    confidence=0.99,
                    polygon=(
                        OCRPoint(x=5, y=5),
                        OCRPoint(x=70, y=5),
                        OCRPoint(x=70, y=25),
                        OCRPoint(x=5, y=25),
                    ),
                    bbox=OCRPixelRect(x_min=5, y_min=5, x_max=70, y_max=25),
                ),
                OCRRegion(
                    region_id="ocr_0001",
                    provider_index=1,
                    raw_text="Bottom bubble",
                    confidence=0.99,
                    polygon=(
                        OCRPoint(x=10, y=45),
                        OCRPoint(x=90, y=45),
                        OCRPoint(x=90, y=70),
                        OCRPoint(x=10, y=70),
                    ),
                    bbox=OCRPixelRect(x_min=10, y_min=45, x_max=90, y_max=70),
                ),
            ),
            evidence=OCRInvocationEvidence(
                provider_id="fake",
                provider_version="1",
                model_id="synthetic",
                request_sha256=semantic_ocr_request_digest(request),
                config_sha256="c" * 64,
            ),
        )


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
            "label": "pr19-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def _seed_panel(app, tmp_path: Path):
    library = app.state.library
    source = tmp_path / "dialogue-api-panel.bin"
    source.write_bytes(b"pr19 dialogue api panel")
    ingested = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    )
    asset = ingested.asset.validated_copy(update={"width": 100, "height": 100})
    ApplicationRepository(library.database).enrich_asset(asset)
    scene = Scene(
        order=0,
        duration_seconds=1.0,
        media=AssetRef(asset_id=asset.asset_id),
    )
    project = library.save_project(
        Project(
            content_kind="panel_sequence",
            state=ProjectState.READY,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
            scenes=(scene,),
        )
    )
    PanelOCRWorkflow(library, _OCRProvider()).extract_scene(project.project_id, scene.scene_id)
    return project, scene


def _register_characters(client: TestClient, headers: dict[str, str], project_id: str) -> None:
    for character_id, display_name in (("alice", "Alice"), ("bob", "Bob")):
        response = client.post(
            f"/api/v1/dialogue/projects/{project_id}/characters",
            headers=headers,
            json={"character_id": character_id, "display_name": display_name},
        )
        assert response.status_code == 201


def test_dialogue_http_surface_keeps_human_assignment_authority(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        project, scene = _seed_panel(app, tmp_path)
        _register_characters(client, headers, project.project_id)

        prepared = client.post(
            f"/api/v1/dialogue/projects/{project.project_id}/scenes/{scene.scene_id}/prepare",
            headers=headers,
            json={
                "suggestions": [
                    {
                        "label": "Assistant proposal",
                        "provider": "fake_assistant",
                        "assignment": {
                            "reading_order": ["ocr_0000", "ocr_0001"],
                            "speaker_by_region": {
                                "ocr_0000": "alice",
                                "ocr_0001": "bob",
                            },
                        },
                    }
                ]
            },
        )
        assert prepared.status_code == 200
        assert prepared.json()["project_state"] == "needs_review"
        assert prepared.json()["dialogue"]["scenes"] == []

        queued = client.get("/api/v1/dialogue/review-queue", headers=headers)
        assert queued.status_code == 200
        items = queued.json()["items"]
        assert len(items) == 1
        task = items[0]["task"]
        assert task["task_type"] == "dialogue_scene_assignment"
        assert task["accepted_value"] is None
        assert len(task["suggestions"]) == 1

        # Generic PR10 resolve cannot acquire authority over the PR19 task.
        generic = client.post(
            f"/api/v1/projects/{project.project_id}/review/{task['review_task_id']}/resolve",
            headers=headers,
            json={"value": task["suggestions"][0]["value"]},
        )
        assert generic.status_code in {409, 422}
        assert app.state.dialogue.manifest(project.project_id).scenes == ()

        accepted = client.post(
            f"/api/v1/dialogue/projects/{project.project_id}/tasks/{task['review_task_id']}/assign",
            headers=headers,
            json={
                "assignment": {
                    "reading_order": ["ocr_0001", "ocr_0000"],
                    "speaker_by_region": {
                        "ocr_0000": "alice",
                        "ocr_0001": "bob",
                    },
                    "focus_hint": {"mode": "speaker"},
                }
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["project_state"] == "ready"
        scene_dialogue = accepted.json()["dialogue"]["scenes"][0]
        assert [line["source_region_id"] for line in scene_dialogue["lines"]] == [
            "ocr_0001",
            "ocr_0000",
        ]
        assert [line["speaker_id"] for line in scene_dialogue["lines"]] == ["bob", "alice"]
        assert scene_dialogue["focus_hint"] == {"mode": "speaker", "face": None, "crop": None}
        assert client.get("/api/v1/dialogue/review-queue", headers=headers).json()["items"] == []
    finally:
        app.state.runtime_lease.close()


def test_dialogue_transport_authenticates_and_bounds_before_json_parsing(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        project, _scene = _seed_panel(app, tmp_path)
        path = f"/api/v1/dialogue/projects/{project.project_id}/characters"

        unauthenticated = client.post(
            path,
            headers={"Content-Type": "application/json"},
            content=b"{not-json",
        )
        assert unauthenticated.status_code == 401

        headers = _paired_headers(client)
        unsupported = client.post(
            path,
            headers={**headers, "Content-Type": "text/plain"},
            content=b"not-json",
        )
        assert unsupported.status_code == 415

        oversized = client.post(
            path,
            headers={
                **headers,
                "Content-Type": "application/json",
                "Content-Length": str(600 * 1024),
            },
            content=b"{}",
        )
        assert oversized.status_code == 413
    finally:
        app.state.runtime_lease.close()


def test_dialogue_queue_quarantines_tampered_assisted_suggestion(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        project, scene = _seed_panel(app, tmp_path)
        _register_characters(client, headers, project.project_id)
        prepared = client.post(
            f"/api/v1/dialogue/projects/{project.project_id}/scenes/{scene.scene_id}/prepare",
            headers=headers,
            json={
                "suggestions": [
                    {
                        "label": "Proposal",
                        "assignment": {
                            "reading_order": ["ocr_0000", "ocr_0001"],
                            "speaker_by_region": {
                                "ocr_0000": "alice",
                                "ocr_0001": "bob",
                            },
                        },
                    }
                ]
            },
        )
        assert prepared.status_code == 200
        current = app.state.library.load_project(project.project_id)
        assert current is not None
        task = next(item for item in current.review_tasks if item.task_type == "dialogue_scene_assignment")
        suggestion = task.suggestions[0]
        tampered = suggestion.validated_copy(
            update={
                "value": {
                    "reading_order": ["ocr_0001", "ocr_0000"],
                    "speaker_by_region": {
                        "ocr_0000": "alice",
                        "ocr_0001": "bob",
                    },
                }
            }
        )
        changed_task = task.validated_copy(update={"suggestions": (tampered,)})
        app.state.library.save_project(
            current.validated_copy(
                update={
                    "review_tasks": tuple(
                        changed_task if item.review_task_id == task.review_task_id else item
                        for item in current.review_tasks
                    )
                }
            )
        )

        queue = client.get("/api/v1/dialogue/review-queue", headers=headers)
        assert queue.status_code == 200
        assert queue.json()["items"] == []
        rejected = client.post(
            f"/api/v1/dialogue/projects/{project.project_id}/tasks/{task.review_task_id}/assign",
            headers=headers,
            json={
                "assignment": {
                    "reading_order": ["ocr_0000", "ocr_0001"],
                    "speaker_by_region": {
                        "ocr_0000": "alice",
                        "ocr_0001": "bob",
                    },
                }
            },
        )
        assert rejected.status_code == 409
        assert "suggestion" in rejected.json()["detail"]
    finally:
        app.state.runtime_lease.close()


def test_pr19_pwa_shell_serves_versioned_dialogue_editor(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/")
        assert shell.status_code == 200
        assert 'id="dialogue-panel"' in shell.text
        assert '<script src="dialogue.js"></script>' in shell.text

        script = client.get("/app/dialogue.js")
        assert script.status_code == 200
        assert "dialogue/review-queue?limit=100" in script.text
        assert "Accept reading order & speakers" in script.text
        assert "Proposal copied into the editor. Nothing has been accepted yet." in script.text
        assert "Cache-Control" in script.headers

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v9" in worker.text
        assert 'appUrl("dialogue.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
