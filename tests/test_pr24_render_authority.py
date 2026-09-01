from __future__ import annotations

import pytest

import content_forge.application.review_pr24_hardening as hardening
from content_forge.application.long_form_shared import LongFormSharedSceneConflictError
from content_forge.application.review import ReviewNotReadyError
from content_forge.application.review_pr23_hardening import ReviewService as PR23ReviewService
from content_forge.application.review_pr24_hardening import ReviewService
from content_forge.core import Project
from content_forge.storage import LocalLibrary


def _service(tmp_path) -> ReviewService:
    return ReviewService(LocalLibrary(tmp_path / "runtime"))


def test_pr24_render_gate_preserves_existing_review_service_identity() -> None:
    assert ReviewService is PR23ReviewService


def test_pr24_render_gate_uses_exact_selected_project_snapshot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    project = Project(content_kind="long_form_fixture")
    sentinel = object()

    class FakeWorkflow:
        def __init__(self, library) -> None:
            assert library is service.library

        def validate_snapshot(self, candidate):
            assert candidate is project
            return None

    monkeypatch.setattr(hardening, "LongFormSharedSceneWorkflow", FakeWorkflow)
    monkeypatch.setattr(
        hardening,
        "_BASE_COMPILE_PLAN",
        lambda self, candidate, profile_id: sentinel,
    )

    assert service._compile_plan(project, "long_form_1080p") is sentinel


def test_pr24_render_gate_blocks_invalid_shared_scene_before_base_compile(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    project = Project(content_kind="long_form_fixture")
    base_called = False

    class FakeWorkflow:
        def __init__(self, library) -> None:
            pass

        def validate_snapshot(self, candidate):
            assert candidate is project
            raise LongFormSharedSceneConflictError("source PR23 authority drifted")

    def base_compile(self, candidate, profile_id):
        nonlocal base_called
        base_called = True
        return object()

    monkeypatch.setattr(hardening, "LongFormSharedSceneWorkflow", FakeWorkflow)
    monkeypatch.setattr(hardening, "_BASE_COMPILE_PLAN", base_compile)

    with pytest.raises(ReviewNotReadyError, match="PR24 shared voiced-scene authority"):
        service._compile_plan(project, "long_form_1080p")
    assert base_called is False
