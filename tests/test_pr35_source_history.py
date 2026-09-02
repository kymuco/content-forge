from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import AssetRef, Project, ProjectState
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
            "label": "pr35-source-history",
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


def test_pr35_used_source_does_not_reappear_when_production_use_is_outside_recent_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        source = _inbox_image(app.state.library, tmp_path, "historical.png", b"historical-source")
        used_project = app.state.production_presets.create_project(
            request_id=str(uuid.uuid4()),
            preset_id="framed_clip",
            source_project_ids=(source.project_id,),
        )
        assert used_project.project_id != source.project_id

        # Simulate the exact production Project having fallen outside the ordinary recent
        # production window. PR35 must still discover its canonical PR32 source snapshot
        # through all-history project_assets candidates for the currently visible source.
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
        assert all(
            item.get("source", {}).get("source_project_id") != source.project_id
            for item in response.json()["items"]
            if item.get("kind") == "source"
        )
    finally:
        app.state.runtime_lease.close()


def test_pr35_duplicate_raw_capture_does_not_count_as_pr32_production_use(
    tmp_path: Path,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        first = _inbox_image(app.state.library, tmp_path, "first.png", b"same-asset")
        second = _inbox_image(app.state.library, tmp_path, "second.png", b"same-asset")
        assert first.source_refs[0].asset_id == second.source_refs[0].asset_id
        assert first.project_id != second.project_id

        response = client.get(
            "/api/v1/production/attention",
            headers=_paired_headers(client),
        )
        assert response.status_code == 200, response.text
        new_source_ids = {
            item["source"]["source_project_id"]
            for item in response.json()["items"]
            if item.get("kind") == "source"
        }
        assert {first.project_id, second.project_id}.issubset(new_source_ids)
    finally:
        app.state.runtime_lease.close()
