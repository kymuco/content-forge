from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


def test_pr26_pwa_shell_serves_and_precaches_production_library(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/")
        assert shell.status_code == 200
        assert 'id="production-library-panel"' in shell.text
        assert '<script src="production-library.js"></script>' in shell.text
        assert "PRODUCTION LIBRARY" in shell.text
        assert "Save current search as a virtual collection" in shell.text
        assert "Check SHA-256 duplicate" in shell.text
        assert 'id="production-library-results"' in shell.text
        assert 'id="production-library-collections"' in shell.text

        script = client.get("/app/production-library.js")
        assert script.status_code == 200
        assert 'apiJson("production-library/search"' in script.text
        assert "production-library/assets/" in script.text
        assert "production-library/collections" in script.text
        assert "production-library/duplicates/" in script.text
        assert "Replace tags" in script.text
        assert "Reuse history" in script.text
        assert "Delete saved query" in script.text
        assert "No existing Asset has this SHA-256." in script.text
        assert script.headers["Cache-Control"] == "no-cache"

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v13" in worker.text
        assert "${CACHE_PREFIX}v14" in worker.text
        assert 'appUrl("production-library.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
