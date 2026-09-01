from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


def test_pr22_pwa_shell_serves_and_precaches_voiced_story_surface(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/")
        assert shell.status_code == 200
        assert 'id="voiced-story-panel"' in shell.text
        assert '<script src="voiced-story.js"></script>' in shell.text
        assert "Preview timing" in shell.text
        assert "Materialize timing" in shell.text

        script = client.get("/app/voiced-story.js")
        assert script.status_code == 200
        assert "voiced-story/projects/" in script.text
        assert "Preview timing" not in script.text  # labels belong to the shell, not JS authority
        assert "materialize" in script.text
        assert 'listen.textContent = "Listen"' in script.text
        assert 'regenerate.textContent = "Regenerate"' in script.text
        assert "/audio`" in script.text
        assert "/regenerate`" in script.text
        assert script.headers["Cache-Control"] == "no-cache"

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v10" in worker.text
        assert "${CACHE_PREFIX}v11" in worker.text
        assert 'appUrl("voiced-story.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
