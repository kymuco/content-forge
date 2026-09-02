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


def test_pr33_crop_controls_remain_phone_first() -> None:
    styles = static_path("styles.css").read_text(encoding="utf-8")

    selector = '#project-flow-panel .stack.compact>.row'
    assert selector in styles
    assert 'grid-template-columns:repeat(2,minmax(0,1fr))' in styles
    assert '@media(min-width:540px)' in styles
    assert 'grid-template-columns:repeat(4,minmax(0,1fr))' in styles


def test_pr33_pr32_source_order_remains_read_only_in_project_flow() -> None:
    script = static_path("production-home.js").read_text(encoding="utf-8")

    assert 'Order is locked from the explicit Create video selection.' in script
    assert 'PR33 does not add a second reorder path.' in script
    assert 'project.production_source_count' in script
    assert 'case "source_order": return "Source order"' in script
    # The only source movement helpers remain the PR32 pre-creation wizard actions. The
    # project flow intentionally exposes no reorder endpoint/task mutation of its own.
    assert 'function moveSelectedSource(sourceId, delta)' in script
    assert 'function moveProjectSource' not in script
    assert 'resolveProjectTask(task, order)' not in script


def test_pr33_terminal_projects_never_offer_false_review_mutations() -> None:
    script = static_path("production-home.js").read_text(encoding="utf-8")

    assert 'let activeProjectState = null' in script
    assert 'activeProjectState = state.toUpperCase()' in script
    assert 'function projectIsTerminal()' in script
    assert 'activeProjectState === "RENDERING"' in script
    assert 'activeProjectState === "QC"' in script
    assert 'activeProjectState === "DONE"' in script
    assert 'function taskIsEditable(task)' in script
    assert 'return taskIsOpen(task) && !projectIsTerminal()' in script
    assert 'if (!taskIsEditable(task))' in script
    assert 'Production has already crossed the final-render boundary' in script
    # READY remains intentionally editable: changing an optional detail there reuses the
    # existing core behavior that invalidates the approved preview and returns to review.
    assert 'activeProjectState === "READY"' not in script.split('function projectIsTerminal()', 1)[1].split('}', 1)[0]


def test_pr33_installed_pwa_namespace_remains_an_explicit_predecessor() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'const PR32_CACHE_NAME = `${CACHE_PREFIX}v18`' in worker
    assert 'const PR33_CACHE_NAME = `${CACHE_PREFIX}v19`' in worker
    assert 'key === PR32_CACHE_NAME' in worker
    assert 'key === PR33_CACHE_NAME' in worker
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
