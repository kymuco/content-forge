from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


def test_pr23_pwa_shell_serves_and_precaches_scene_presentation(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/")
        assert shell.status_code == 200
        assert 'id="voiced-scene-panel"' in shell.text
        assert '<script src="voiced-scene.js"></script>' in shell.text
        assert "Preview presentation" in shell.text
        assert "Materialize presentation" in shell.text
        assert "Remove presentation" in shell.text

        script = client.get("/app/voiced-scene.js")
        assert script.status_code == 200
        assert "voiced-scene/projects/" in script.text
        assert "/materialize`" in script.text
        assert "/materialization`" in script.text
        assert "blocking QC" in script.text
        assert script.headers["Cache-Control"] == "no-cache"

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v11" in worker.text
        assert "${CACHE_PREFIX}v12" in worker.text
        assert 'appUrl("voiced-scene.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
