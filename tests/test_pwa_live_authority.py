from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


LOOPBACK_HEADERS = {"Host": "localhost"}


def test_live_pwa_config_json_matches_server_authority_and_is_not_cacheable(tmp_path) -> None:
    app = create_app(root=tmp_path, max_upload_bytes=32)
    client = TestClient(app)
    try:
        response = client.get("/app/config.json", headers=LOOPBACK_HEADERS)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {
            "maxUploadBytes": 32,
            "maxShareBodyBytes": 32 + 1024 * 1024,
            "maxQueueBytes": 32,
            "maxQueueEntries": 256,
            "maxBatchEntries": 16,
            "maxFilenameChars": 1024,
            "maxMimeChars": 255,
            "maxUrlChars": 4096,
            "maxNoteChars": 8192,
        }
    finally:
        app.state.runtime_lease.close()
