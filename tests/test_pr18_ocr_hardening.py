from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.application import (
    ApplicationRepository,
    PanelOCRConflictError,
    PanelOCRWorkflow,
    prepare_panel_ocr,
)
from content_forge.core import AssetRef, MediaType, Project, ProjectState, Scene
from content_forge.providers import (
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRRegion,
    OCRRequest,
    OCRResult,
    semantic_ocr_request_digest,
)
from content_forge.storage import LocalLibrary


def _region(region_id: str, index: int, text: str, confidence: float) -> OCRRegion:
    y = 5.0 + 30.0 * index
    return OCRRegion(
        region_id=region_id,
        provider_index=index,
        raw_text=text,
        confidence=confidence,
        polygon=(
            OCRPoint(x=5, y=y),
            OCRPoint(x=80, y=y),
            OCRPoint(x=80, y=y + 20),
            OCRPoint(x=5, y=y + 20),
        ),
        bbox=OCRPixelRect(x_min=5, y_min=y, x_max=80, y_max=y + 20),
    )


def _evidence(request_sha256: str = "a" * 64) -> OCRInvocationEvidence:
    return OCRInvocationEvidence(
        provider_id="fake_local",
        provider_version="1.0",
        model_id="synthetic",
        engine="fake",
        request_sha256=request_sha256,
        config_sha256="b" * 64,
    )


def test_high_confidence_panel_ocr_needs_no_review_task() -> None:
    asset_id = "cf_asset_" + "1" * 32
    scene = Scene(
        scene_id="cf_scene_" + "2" * 32,
        order=0,
        duration_seconds=1.0,
        media=AssetRef(asset_id=asset_id),
    )
    project = Project(
        project_id="cf_project_" + "3" * 32,
        content_kind="panel_sequence",
        source_refs=(AssetRef(asset_id=asset_id),),
        scenes=(scene,),
    )
    result = OCRResult(
        source_sha256="c" * 64,
        width=100,
        height=200,
        regions=(
            _region("ocr_0000", 0, "Certain one", 0.99),
            _region("ocr_0001", 1, "Certain two", 0.95),
        ),
        evidence=_evidence(),
    )

    prepared = prepare_panel_ocr(
        project,
        scene.scene_id,
        result,
        review_confidence_threshold=0.80,
    )

    assert prepared.extraction.uncertain_region_ids == ()
    assert prepared.review_task is None


class _Provider:
    def __init__(self, template: OCRResult) -> None:
        self.template = template
        self.calls = 0

    def health(self):  # pragma: no cover
        raise AssertionError

    def extract(self, request: OCRRequest) -> OCRResult:
        self.calls += 1
        evidence = self.template.evidence.validated_copy(
            update={"request_sha256": semantic_ocr_request_digest(request)}
        )
        return self.template.validated_copy(
            update={
                "source_sha256": request.source_sha256,
                "width": request.width,
                "height": request.height,
                "evidence": evidence,
            }
        )


def _stored_panel(tmp_path: Path, *, state: ProjectState = ProjectState.DRAFT):
    library = LocalLibrary(tmp_path)
    source = tmp_path / "panel.bin"
    source.write_bytes(b"retained OCR policy fixture")
    ingested = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    )
    asset = ingested.asset.validated_copy(update={"width": 100, "height": 200})
    ApplicationRepository(library.database).enrich_asset(asset)
    scene = Scene(
        order=0,
        duration_seconds=1.0,
        media=AssetRef(asset_id=asset.asset_id),
    )
    project = library.save_project(
        Project(
            content_kind="panel_sequence",
            state=state,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
            scenes=(scene,),
        )
    )
    template = OCRResult(
        source_sha256=asset.sha256,
        width=100,
        height=200,
        regions=(_region("ocr_0000", 0, "Ambiguous", 0.60),),
        evidence=_evidence(),
    )
    provider = _Provider(template)
    return library, project, scene, provider


def test_retained_ocr_rejects_changed_review_threshold(tmp_path: Path) -> None:
    library, project, scene, provider = _stored_panel(tmp_path)
    workflow = PanelOCRWorkflow(library, provider)
    workflow.extract_scene(
        project.project_id,
        scene.scene_id,
        language_hints=("en",),
        review_confidence_threshold=0.80,
    )

    with pytest.raises(PanelOCRConflictError, match="confidence threshold"):
        workflow.extract_scene(
            project.project_id,
            scene.scene_id,
            language_hints=("en",),
            review_confidence_threshold=0.90,
        )
    assert provider.calls == 1


def test_retained_ocr_rejects_changed_semantic_language_hints(tmp_path: Path) -> None:
    library, project, scene, provider = _stored_panel(tmp_path)
    workflow = PanelOCRWorkflow(library, provider)
    workflow.extract_scene(
        project.project_id,
        scene.scene_id,
        language_hints=("en",),
    )

    with pytest.raises(PanelOCRConflictError, match="semantic request hints"):
        workflow.extract_scene(
            project.project_id,
            scene.scene_id,
            language_hints=("ja",),
        )
    assert provider.calls == 1


def test_uncertain_ocr_moves_ready_project_to_review_then_restores_ready(
    tmp_path: Path,
) -> None:
    library, project, scene, provider = _stored_panel(tmp_path, state=ProjectState.READY)
    workflow = PanelOCRWorkflow(library, provider)

    extracted = workflow.extract_scene(project.project_id, scene.scene_id)
    assert extracted.state is ProjectState.NEEDS_REVIEW
    task = next(task for task in extracted.review_tasks if task.task_type == "ocr_text_correction")
    assert task.payload["resume_state"] == ProjectState.READY.value

    corrected = workflow.apply_corrections(
        project.project_id,
        task.review_task_id,
        {"ocr_0000": "Accepted"},
    )
    assert corrected.state is ProjectState.READY


def test_done_project_rejects_ocr_before_provider_execution(tmp_path: Path) -> None:
    library, project, scene, provider = _stored_panel(tmp_path, state=ProjectState.DONE)
    workflow = PanelOCRWorkflow(library, provider)

    with pytest.raises(PanelOCRConflictError, match="state done"):
        workflow.extract_scene(project.project_id, scene.scene_id)
    assert provider.calls == 0
