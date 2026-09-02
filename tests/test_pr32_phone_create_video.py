from __future__ import annotations

import shutil
import subprocess

import pytest

from content_forge.web import static_path


def test_pr32_phone_home_contains_real_create_video_wizard_contract() -> None:
    script = static_path("production-home.js").read_text(encoding="utf-8")

    assert 'createVideoButton.id = "production-home-create"' in script
    assert 'createPanel.id = "create-video-panel"' in script
    assert 'createProjectButton.id = "create-video-submit"' in script
    assert 'apiJson("production/presets")' in script
    assert 'apiJson(`production/sources?limit=${SOURCE_LIMIT}`)' in script
    assert 'apiJson("production/projects?limit=100")' in script
    assert 'apiJson("production/projects", {' in script
    assert 'preset_id: selectedPreset.preset_id' in script
    assert 'source_project_ids: selectedSourceIds' in script
    assert 'crypto.randomUUID()' in script
    assert 'Retry is safe with the same request identity.' in script
    assert 'selectedSourceIds = [...selectedSourceIds, sourceId]' in script
    assert 'moveSelectedSource(source.source_project_id, -1)' in script
    assert 'moveSelectedSource(source.source_project_id, 1)' in script
    assert 'project.production_preset_label' in script
    assert 'project.production_source_count' in script
    assert "innerHTML" not in script


def test_pr32_installed_pwa_upgrades_pr31_shell_without_forgetting_historical_cache_names() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'const PR29_CACHE_NAME = `${CACHE_PREFIX}v16`' in worker
    assert 'const PR31_CACHE_NAME = `${CACHE_PREFIX}v17`' in worker
    assert 'const CACHE_NAME = `${CACHE_PREFIX}v18`' in worker
    assert 'key === PR29_CACHE_NAME' in worker
    assert 'key === PR31_CACHE_NAME' in worker
    assert 'appUrl("production-home.js")' in worker


def test_pr32_phone_controller_has_valid_javascript_syntax() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    completed = subprocess.run(
        [node, "--check", str(static_path("production-home.js"))],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
