from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


def test_pr21_pwa_shell_serves_and_precaches_voice_cast_surface(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/")
        assert shell.status_code == 200
        assert 'id="voice-cast-panel"' in shell.text
        assert '<script src="voice-cast.js"></script>' in shell.text

        script = client.get("/app/voice-cast.js")
        assert script.status_code == 200
        assert "Assign latest" in script.text
        assert "Preview voice" in script.text
        assert "voice-cast/projects/" in script.text
        assert "Cache-Control" in script.headers

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v9" in worker.text
        assert "${CACHE_PREFIX}v10" in worker.text
        assert 'appUrl("voice-cast.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
