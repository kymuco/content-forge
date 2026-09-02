from __future__ import annotations

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.web import static_path

LOOPBACK_HEADERS = {"Host": "localhost"}


def test_phone_production_home_is_the_primary_paired_work_surface(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        shell = client.get("/app/", headers=LOOPBACK_HEADERS)
        assert shell.status_code == 200
        assert 'id="production-home-panel"' in shell.text
        assert 'id="production-home-projects"' in shell.text
        assert 'id="production-home-add"' in shell.text
        assert 'id="production-home-advanced"' in shell.text
        assert '<script src="production-home.js"></script>' in shell.text
        assert shell.text.index('id="production-home-panel"') < shell.text.index('id="capture-panel"')
        assert shell.text.index('id="production-home-panel"') < shell.text.index('id="review-panel"')
        assert shell.text.index('src="review.js"') < shell.text.index('src="production-home.js"')

        controller = client.get("/app/production-home.js", headers=LOOPBACK_HEADERS)
        assert controller.status_code == 200
        assert controller.headers["content-type"].startswith("text/javascript")
        assert "default-src 'self'" in controller.headers["content-security-policy"]
    finally:
        app.state.runtime_lease.close()


def test_phone_home_reuses_existing_authority_without_a_parallel_product_state() -> None:
    controller = static_path("production-home.js").read_text(encoding="utf-8")
    service_worker = static_path("sw.js").read_text(encoding="utf-8")

    # The daily-use projection reads existing Inbox/review/project authority.
    assert 'apiJson("inbox?limit=100")' in controller
    assert 'apiJson("review-queue?limit=100")' in controller
    assert 'apiJson(`projects/${encodeURIComponent(projectId)}`)' in controller

    # Start/final/watch actions cross only the already-governed PR10 render/review boundary.
    assert '/review/bootstrap`' in controller
    assert '/final`' in controller
    assert 'project.final.artifact_endpoint' in controller
    assert 'Authorization' in controller

    # Internal IDs are used only for API addressing; cards use source/content labels.
    assert "intakeLabel(intake, project)" in controller
    assert 'text("strong", intakeLabel(intake, project))' in controller
    assert 'text("strong", project.project_id)' not in controller
    assert "innerHTML" not in controller

    # Existing subsystem control surfaces remain available but default behind Advanced.
    for panel_id in (
        "dialogue-panel",
        "voice-cast-panel",
        "voiced-story-panel",
        "voiced-scene-panel",
        "production-profile-panel",
        "production-library-panel",
        "publishing-panel",
        "inbox-panel",
    ):
        assert f'"{panel_id}"' in controller
    assert 'advancedVisible = false' in controller

    # Installed PWAs must upgrade to the new product shell instead of remaining on v16,
    # while retaining v16 as an explicit predecessor so historical upgrade contracts hold.
    assert 'const PR29_CACHE_NAME = `${CACHE_PREFIX}v16`' in service_worker
    assert 'const CACHE_NAME = `${CACHE_PREFIX}v17`' in service_worker
    assert 'key === PR29_CACHE_NAME' in service_worker
    assert 'appUrl("production-home.js")' in service_worker


def test_phone_home_javascript_parses_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed; GitHub Actions provides it for the syntax gate")
    completed = subprocess.run(
        [node, "--check", str(static_path("production-home.js"))],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
