from __future__ import annotations

import shutil
import subprocess

import pytest

from content_forge.web import static_path


def test_pr33_project_flow_reuses_existing_review_preview_final_authority() -> None:
    script = static_path("production-home.js").read_text(encoding="utf-8")

    assert 'projectFlowPanel.id = "project-flow-panel"' in script
    assert 'activeProjectId = projectId' in script
    assert 'apiJson(`projects/${encodeURIComponent(projectId)}`)' in script
    assert (
        'projects/${encodeURIComponent(activeProjectId)}/review/${encodeURIComponent(task.review_task_id)}/resolve'
        in script
    )
    assert 'apiJson(`projects/${encodeURIComponent(activeProjectId)}/preview`, { method: "POST" })' in script
    assert '/preview/${encodeURIComponent(preview.job_id)}/approve`' in script
    assert '/preview/${encodeURIComponent(preview.job_id)}/reject`' in script
    assert 'apiJson(`projects/${encodeURIComponent(activeProjectId)}/final`' in script
    assert 'attachProjectArtifact' in script
    assert 'project.final.artifact_endpoint' in script
    assert 'Authorization' in script
    assert "innerHTML" not in script


def test_pr33_home_opens_one_project_context_instead_of_global_review_shortcuts() -> None:
    script = static_path("production-home.js").read_text(encoding="utf-8")

    assert 'button("Continue", "primary", () => openProject(project.project_id, label))' in script
    assert 'button("View progress", "secondary", () => openProject(project.project_id, label))' in script
    assert 'button("View final", "primary", () => openProject(project.project_id, label))' in script
    assert 'await openProject(result.project_id, label)' in script
    assert '"capture-panel"' in script
    assert '"review-panel"' in script
    assert 'advancedVisible = false' in script
    assert 'text("strong", project.project_id)' not in script


def test_pr33_crop_editor_is_bounded_and_stays_on_existing_crop_contract() -> None:
    script = static_path("production-home.js").read_text(encoding="utf-8")

    assert 'function validatedCrop(inputs)' in script
    assert 'fullLabel.append(full, document.createTextNode(" Use full frame"))' in script
    assert 'input.min = "0"' in script
    assert 'input.max = "1"' in script
    assert 'input.step = "0.01"' in script
    assert 'x + width > 1.000001' in script
    assert 'y + height > 1.000001' in script
    assert 'crops[editor.sceneId] = editor.full.checked ? null : validatedCrop(editor)' in script
    assert 'await resolveProjectTask(task, { crops })' in script


def test_pr33_pr32_source_order_remains_read_only_in_project_flow() -> None:
    script = static_path("production-home.js").read_text(encoding="utf-8")

    assert 'Order is locked from the explicit Create video selection.' in script
    assert 'PR33 does not add a second reorder path.' in script
    assert 'project.production_source_count' in script
    # PR32 wizard still owns source reordering before project creation. PR33 must not add
    # a new task resolver for source_order inside the project-specific editor.
    assert 'case "source_order": return renderReadOnlyDecision(task)' not in script
    assert 'case "source_order"' not in script


def test_pr33_installed_pwa_advances_from_pr32_shell() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'const PR32_CACHE_NAME = `${CACHE_PREFIX}v18`' in worker
    assert 'const CACHE_NAME = `${CACHE_PREFIX}v19`' in worker
    assert 'key === PR32_CACHE_NAME' in worker
    assert 'appUrl("production-home.js")' in worker


def test_pr33_phone_controller_has_valid_javascript_syntax() -> None:
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
