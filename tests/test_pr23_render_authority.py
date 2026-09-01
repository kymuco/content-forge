from __future__ import annotations

from types import SimpleNamespace

import pytest

from content_forge.application.review import ReviewNotReadyError
from content_forge.application.review_pr23_hardening import ReviewService
from content_forge.application.review_seventh_hardening import ReviewService as BaseReviewService
from content_forge.core import Project
from content_forge.storage import LocalLibrary
import content_forge.application.review_pr23_hardening as hardening


def _service(tmp_path) -> ReviewService:
    return ReviewService(LocalLibrary(tmp_path / "runtime"))


def test_pr23_render_gate_leaves_non_voiced_projects_on_existing_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    project = Project(content_kind="plain_fixture")
    sentinel = object()
    monkeypatch.setattr(hardening, "voiced_story_manifest", lambda candidate: None)
    monkeypatch.setattr(
        BaseReviewService,
        "_compile_plan",
        lambda self, candidate, profile_id: sentinel,
    )

    assert service._compile_plan(project, "preview") is sentinel


def test_pr23_render_gate_blocks_materialized_pr22_without_pr23(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    project = Project(content_kind="voiced_fixture")
    monkeypatch.setattr(hardening, "voiced_story_manifest", lambda candidate: object())
    monkeypatch.setattr(hardening, "voiced_scene_manifest", lambda candidate: None)

    with pytest.raises(ReviewNotReadyError, match="requires PR23 presentation"):
        service._compile_plan(project, "preview")


def test_pr23_render_gate_accepts_exact_rederived_presentation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    project = Project(content_kind="voiced_fixture")
    preset = object()
    plan = SimpleNamespace(preset=preset)
    stored = SimpleNamespace(plan=plan)
    sentinel = object()

    class FakeWorkflow:
        def __init__(self, library) -> None:
            assert library is service.library

        def _base_project(self, candidate, manifest):
            assert candidate is project
            assert manifest is stored
            return candidate

        def derive(self, candidate, *, preset):
            assert candidate is project
            assert preset is plan.preset
            return plan

    monkeypatch.setattr(hardening, "voiced_story_manifest", lambda candidate: object())
    monkeypatch.setattr(hardening, "voiced_scene_manifest", lambda candidate: stored)
    monkeypatch.setattr(hardening, "VoicedSceneWorkflow", FakeWorkflow)
    monkeypatch.setattr(
        BaseReviewService,
        "_compile_plan",
        lambda self, candidate, profile_id: sentinel,
    )

    assert service._compile_plan(project, "preview") is sentinel


def test_pr23_render_gate_blocks_stale_presentation_before_base_compile(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    project = Project(content_kind="voiced_fixture")
    plan = SimpleNamespace(preset=object())
    stored = SimpleNamespace(plan=plan)
    base_called = False

    class FakeWorkflow:
        def __init__(self, library) -> None:
            pass

        def _base_project(self, candidate, manifest):
            return candidate

        def derive(self, candidate, *, preset):
            return SimpleNamespace(preset=preset)

    def base_compile(self, candidate, profile_id):
        nonlocal base_called
        base_called = True
        return object()

    monkeypatch.setattr(hardening, "voiced_story_manifest", lambda candidate: object())
    monkeypatch.setattr(hardening, "voiced_scene_manifest", lambda candidate: stored)
    monkeypatch.setattr(hardening, "VoicedSceneWorkflow", FakeWorkflow)
    monkeypatch.setattr(BaseReviewService, "_compile_plan", base_compile)

    with pytest.raises(ReviewNotReadyError, match="stale"):
        service._compile_plan(project, "final")
    assert base_called is False
