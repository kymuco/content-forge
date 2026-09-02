from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.web import static_path


def test_pr35_served_production_home_composes_attention_without_replacing_project_authority(
    tmp_path: Path,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        response = client.get("/app/production-home.js")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        script = response.text
        assert "content-forge:project-flow-rendered" in script
        assert "content-forge:production-home-refreshed" in script
        assert "window.CFProductionHome = Object.freeze" in script
        assert "openProject," in script
        assert "openCreateVideo: openCreateWizard" in script
        assert 'apiJson("publishing/status")' in script
        assert 'apiJson("production/attention?limit=100")' in script
        assert 'apiJson("production/safe-work"' in script
        assert "Run safe work" in script
        assert "Needs recovery" in script
        assert "Needs you" in script
        assert "Ready automatically" in script
        assert "New sources" in script
        assert "Finished" in script
    finally:
        app.state.runtime_lease.close()


def test_pr35_attention_module_has_no_human_authority_or_remote_execute_mutations() -> None:
    script = static_path("attention-queue.js").read_text(encoding="utf-8")

    assert "production/safe-work" in script
    assert "production/attention?limit=100" in script
    assert "publishing/status" in script
    assert "/review/" not in script
    assert "preview/approve" not in script
    assert "preview/reject" not in script
    assert "publishing/candidates" not in script
    assert "publishing/attempts" not in script
    assert "/execute" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "innerHTML" not in script


def test_pr35_installed_pwa_advances_from_pr34_shell() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'const PR33_CACHE_NAME = `${CACHE_PREFIX}v19`' in worker
    assert 'const PR34_CACHE_NAME = `${CACHE_PREFIX}v20`' in worker
    assert 'const CACHE_NAME = `${CACHE_PREFIX}v21`' in worker
    assert "key === PR34_CACHE_NAME" in worker
    assert 'appUrl("production-home.js")' in worker


def test_pr35_attention_and_composed_home_javascript_have_valid_syntax(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")

    attention = subprocess.run(
        [node, "--check", str(static_path("attention-queue.js"))],
        check=False,
        capture_output=True,
        text=True,
    )
    assert attention.returncode == 0, attention.stderr

    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    try:
        response = client.get("/app/production-home.js")
        assert response.status_code == 200
        composed_path = tmp_path / "production-home-pr35.js"
        composed_path.write_text(response.text, encoding="utf-8")
        composed = subprocess.run(
            [node, "--check", str(composed_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert composed.returncode == 0, composed.stderr
    finally:
        app.state.runtime_lease.close()
