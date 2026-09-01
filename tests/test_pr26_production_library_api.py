from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import AssetRef, EntityKind, Project, new_entity_id
from content_forge.storage import SourceInput

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
            "label": "pr26-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def test_pr26_library_api_tag_search_duplicate_reuse_and_collections(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        ingest = app.state.library.assets.ingest_file(
            _write(tmp_path / "panel.png", b"panel-api"),
            source=SourceInput(
                source_url="https://example.invalid/post",
                platform="artist_site",
            ),
        )
        assert ingest.source_record is not None
        asset_id = ingest.asset.asset_id

        replaced = client.put(
            f"/api/v1/production-library/assets/{asset_id}/tags",
            headers=headers,
            json={
                "tags": [
                    {"kind": "game", "value": "Genshin Impact"},
                    {"kind": "character", "value": "Raiden Shogun"},
                ]
            },
        )
        assert replaced.status_code == 200
        assert replaced.json()["asset_id"] == asset_id
        assert {item["value"] for item in replaced.json()["tags"]} == {
            "Genshin Impact",
            "Raiden Shogun",
        }

        fetched_tags = client.get(
            f"/api/v1/production-library/assets/{asset_id}/tags",
            headers=headers,
        )
        assert fetched_tags.status_code == 200
        assert fetched_tags.json() == replaced.json()

        search = client.post(
            "/api/v1/production-library/search",
            headers=headers,
            json={
                "tags": [{"kind": "character", "value": "raiden shogun"}],
                "previously_used": False,
            },
        )
        assert search.status_code == 200
        assert [item["asset"]["asset_id"] for item in search.json()["items"]] == [asset_id]
        assert search.json()["items"][0]["project_count"] == 0

        duplicate = client.get(
            f"/api/v1/production-library/duplicates/{ingest.asset.sha256.upper()}",
            headers=headers,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["match"]["asset"]["asset_id"] == asset_id
        assert duplicate.json()["match"]["source_count"] == 1

        project = Project(
            content_kind="library_api_fixture",
            source_refs=(
                AssetRef(
                    asset_id=asset_id,
                    source_id=ingest.source_record.source_id,
                    role="panel",
                ),
            ),
            source_records=(ingest.source_record,),
        )
        app.state.library.save_project(project)
        reuse = client.get(
            f"/api/v1/production-library/assets/{asset_id}/reuse",
            headers=headers,
        )
        assert reuse.status_code == 200
        assert reuse.json()["items"][0]["project_id"] == project.project_id
        assert reuse.json()["items"][0]["role"] == "panel"

        collection = client.put(
            "/api/v1/production-library/collections/raiden_panels",
            headers=headers,
            json={
                "name": "Raiden panels",
                "query": {"tags": [{"kind": "character", "value": "Raiden Shogun"}]},
            },
        )
        assert collection.status_code == 200
        assert collection.json()["collection_id"] == "raiden_panels"

        listed = client.get("/api/v1/production-library/collections", headers=headers)
        assert listed.status_code == 200
        assert [item["collection_id"] for item in listed.json()["items"]] == ["raiden_panels"]

        items = client.get(
            "/api/v1/production-library/collections/raiden_panels/items",
            headers=headers,
        )
        assert items.status_code == 200
        assert [item["asset"]["asset_id"] for item in items.json()["items"]] == [asset_id]

        removed = client.delete(
            "/api/v1/production-library/collections/raiden_panels",
            headers=headers,
        )
        assert removed.status_code == 200
        assert removed.json() == {"collection_id": "raiden_panels", "deleted": True}
        missing = client.get(
            "/api/v1/production-library/collections/raiden_panels",
            headers=headers,
        )
        assert missing.status_code == 404
    finally:
        app.state.runtime_lease.close()


def test_pr26_library_api_validates_ids_and_duplicate_misses(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        malformed = client.get(
            "/api/v1/production-library/assets/not-an-asset/tags",
            headers=headers,
        )
        assert malformed.status_code == 422

        unknown_id = new_entity_id(EntityKind.ASSET)
        unknown = client.get(
            f"/api/v1/production-library/assets/{unknown_id}/tags",
            headers=headers,
        )
        assert unknown.status_code == 404

        bad_digest = client.get(
            "/api/v1/production-library/duplicates/not-a-digest",
            headers=headers,
        )
        assert bad_digest.status_code == 422
        no_match = client.get(
            f"/api/v1/production-library/duplicates/{'0' * 64}",
            headers=headers,
        )
        assert no_match.status_code == 200
        assert no_match.json()["match"] is None

        invalid_collection = client.get(
            "/api/v1/production-library/collections/Not Valid!",
            headers=headers,
        )
        assert invalid_collection.status_code == 422
    finally:
        app.state.runtime_lease.close()


def test_pr26_library_api_auth_precedes_json_and_enforces_body_cap(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    try:
        unauthenticated = client.post(
            "/api/v1/production-library/search",
            headers=LOOPBACK_HEADERS,
            content=b"{not-json",
        )
        assert unauthenticated.status_code == 401

        headers = _paired_headers(client)
        oversized = client.post(
            "/api/v1/production-library/search",
            headers={
                **headers,
                "Content-Type": "application/json",
                "Content-Length": str(65 * 1024),
            },
            content=b"{}",
        )
        assert oversized.status_code == 413
    finally:
        app.state.runtime_lease.close()


def test_pr26_route_install_does_not_eagerly_initialize_feature_schema(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "runtime")
    try:
        with app.state.library.database.connection() as connection:
            row = connection.execute(
                """
                SELECT version FROM application_schema
                WHERE component = 'production_library'
                """
            ).fetchone()
        assert row is None
    finally:
        app.state.runtime_lease.close()
