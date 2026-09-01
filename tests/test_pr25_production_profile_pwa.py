from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


def test_pr25_pwa_shell_serves_and_precaches_production_profiles(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/")
        assert shell.status_code == 200
        assert 'id="production-profile-panel"' in shell.text
        assert '<script src="production-profiles.js"></script>' in shell.text
        assert "Bind / rebind" in shell.text
        assert "Unbind" in shell.text

        script = client.get("/app/production-profiles.js")
        assert script.status_code == 200
        assert 'apiJson("production-profiles")' in script.text
        assert "production-profiles/projects/" in script.text
        assert "PR25-owned defaults" in script.text
        assert script.headers["Cache-Control"] == "no-cache"

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v12" in worker.text
        assert "${CACHE_PREFIX}v13" in worker.text
        assert 'appUrl("production-profiles.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
