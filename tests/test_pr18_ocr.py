from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.application import (
    ApplicationRepository,
    PanelOCRConflictError,
    PanelOCRValidationError,
    PanelOCRWorkflow,
)
from content_forge.core import AssetRef, MediaType, Project, ReviewStatus, Scene
from content_forge.providers import (
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRRegion,
    OCRRequest,
    OCRResponseError,
    OCRResult,
    PaddleOCRConfig,
    PaddleOCRProvider,
    semantic_ocr_request_digest,
)
from content_forge.storage import LocalLibrary


class _Array:
    def __init__(self, value):
        self.value = value

    def tolist(self):
        return self.value


class _Result:
    def __init__(self, payload):
        self.json = {"res": payload}


class _Runtime:
    def __init__(self, payload):
        self.payload = payload
        self.paths: list[str] = []

    def predict(self, path: str):
        self.paths.append(path)
        return [_Result(self.payload)]


def _request(path: Path) -> OCRRequest:
    return OCRRequest(
        image_path=path,
        source_sha256="a" * 64,
        width=100,
        height=200,
        language_hints=("ja", "en"),
    )


def _payload():
    return {
        "rec_texts": ["Hello", "  ", "世界"],
        "rec_scores": _Array([0.98, 0.11, 0.42]),
        "rec_polys": _Array(
            [
                [[1, 2], [31, 2], [31, 20], [1, 20]],
                [[2, 30], [12, 30], [12, 40], [2, 40]],
                [[10, 50], [70, 50], [70, 90], [10, 90]],
            ]
        ),
        "rec_boxes": _Array(
            [
                [1, 2, 31, 20],
                [2, 30, 12, 40],
                [10, 50, 70, 90],
            ]
        ),
    }


def test_ocr_semantic_digest_excludes_runtime_path(tmp_path: Path) -> None:
    first = _request(tmp_path / "a.png")
    second = first.validated_copy(update={"image_path": tmp_path / "other.png"})
    assert semantic_ocr_request_digest(first) == semantic_ocr_request_digest(second)


def test_paddleocr_adapter_normalizes_array_results_and_preserves_provider_index(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(_payload())
    provider = PaddleOCRProvider(
        PaddleOCRConfig(lang="japan", device="cpu"),
        runtime_factory=lambda _config: runtime,
        provider_version="3.7.0",
    )

    result = provider.extract(_request(tmp_path / "panel.png"))

    assert provider.health().available is True
    assert runtime.paths == [str(tmp_path / "panel.png")]
    assert [region.raw_text for region in result.regions] == ["Hello", "世界"]
    assert [region.provider_index for region in result.regions] == [0, 2]
    assert result.regions[0].bbox == OCRPixelRect(x_min=1, y_min=2, x_max=31, y_max=20)
    assert result.regions[1].confidence == pytest.approx(0.42)
    assert result.evidence.provider_id == "paddleocr_local"
    assert result.evidence.provider_version == "3.7.0"
    assert result.evidence.model_id == "PP-OCRv6"


def test_paddleocr_adapter_rejects_mismatched_arrays(tmp_path: Path) -> None:
    payload = _payload()
    payload["rec_scores"] = _Array([0.9])
    provider = PaddleOCRProvider(
        runtime_factory=lambda _config: _Runtime(payload),
        provider_version="3.7.0",
    )
    with pytest.raises(OCRResponseError, match="different lengths"):
        provider.extract(_request(tmp_path / "panel.png"))


def test_paddleocr_adapter_rejects_source_geometry_overflow(tmp_path: Path) -> None:
    payload = _payload()
    payload["rec_boxes"] = _Array(
        [[1, 2, 131, 20], [2, 30, 12, 40], [10, 50, 70, 90]]
    )
    payload["rec_polys"] = _Array(
        [
            [[1, 2], [131, 2], [131, 20], [1, 20]],
            [[2, 30], [12, 30], [12, 40], [2, 40]],
            [[10, 50], [70, 50], [70, 90], [10, 90]],
        ]
    )
    provider = PaddleOCRProvider(
        runtime_factory=lambda _config: _Runtime(payload),
        provider_version="3.7.0",
    )
    with pytest.raises(OCRResponseError, match="source geometry"):
        provider.extract(_request(tmp_path / "panel.png"))


class _FakeProvider:
    def __init__(self, result: OCRResult) -> None:
        self.result = result
        self.calls = 0

    def health(self):  # pragma: no cover - workflow does not need a health call
        raise AssertionError

    def extract(self, request: OCRRequest) -> OCRResult:
        self.calls += 1
        return self.result.validated_copy(
            update={
                "source_sha256": request.source_sha256,
                "width": request.width,
                "height": request.height,
            }
        )


def _result(source_sha256: str, width: int, height: int) -> OCRResult:
    evidence = OCRInvocationEvidence(
        provider_id="fake_local",
        provider_version="1.0",
        model_id="synthetic",
        engine="fake",
        request_sha256="b" * 64,
        config_sha256="c" * 64,
    )
    return OCRResult(
        source_sha256=source_sha256,
        width=width,
        height=height,
        regions=(
            OCRRegion(
                region_id="ocr_0000",
                provider_index=0,
                raw_text="Certain line",
                confidence=0.99,
                polygon=(
                    OCRPoint(x=1, y=1),
                    OCRPoint(x=80, y=1),
                    OCRPoint(x=80, y=20),
                    OCRPoint(x=1, y=20),
                ),
                bbox=OCRPixelRect(x_min=1, y_min=1, x_max=80, y_max=20),
            ),
            OCRRegion(
                region_id="ocr_0001",
                provider_index=1,
                raw_text="Raw uncertain",
                confidence=0.55,
                polygon=(
                    OCRPoint(x=5, y=30),
                    OCRPoint(x=90, y=30),
                    OCRPoint(x=90, y=55),
                    OCRPoint(x=5, y=55),
                ),
                bbox=OCRPixelRect(x_min=5, y_min=30, x_max=90, y_max=55),
            ),
        ),
        evidence=evidence,
    )


def _library_project(tmp_path: Path):
    library = LocalLibrary(tmp_path)
    source = tmp_path / "panel.bin"
    source.write_bytes(b"synthetic panel bytes")
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
            source_refs=(AssetRef(asset_id=asset.asset_id),),
            scenes=(scene,),
        )
    )
    return library, project, scene, asset


def test_panel_ocr_workflow_retains_raw_text_and_resolves_uncertain_review(
    tmp_path: Path,
) -> None:
    library, project, scene, asset = _library_project(tmp_path)
    provider = _FakeProvider(_result(asset.sha256, 100, 200))
    workflow = PanelOCRWorkflow(library, provider)

    extracted = workflow.extract_scene(
        project.project_id,
        scene.scene_id,
        language_hints=("en",),
        review_confidence_threshold=0.80,
    )
    assert provider.calls == 1
    tasks = [task for task in extracted.review_tasks if task.task_type == "ocr_text_correction"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task.status is ReviewStatus.OPEN
    assert task.payload["uncertain_region_ids"] == ["ocr_0001"]

    before = workflow.extraction(project.project_id, scene.scene_id)
    assert before.regions[1].raw_text == "Raw uncertain"
    assert before.regions[1].corrected_text is None

    corrected_project = workflow.apply_corrections(
        project.project_id,
        task.review_task_id,
        {"ocr_0001": "Corrected uncertain"},
    )
    corrected = workflow.extraction(project.project_id, scene.scene_id)
    assert corrected.regions[1].raw_text == "Raw uncertain"
    assert corrected.regions[1].corrected_text == "Corrected uncertain"
    assert corrected.regions[1].effective_text == "Corrected uncertain"
    resolved = next(
        item
        for item in corrected_project.review_tasks
        if item.review_task_id == task.review_task_id
    )
    assert resolved.status is ReviewStatus.RESOLVED
    assert resolved.accepted_value["corrections"] == {"ocr_0001": "Corrected uncertain"}

    with pytest.raises(PanelOCRConflictError, match="already closed"):
        workflow.apply_corrections(
            project.project_id,
            task.review_task_id,
            {"ocr_0001": "Another value"},
        )


def test_panel_ocr_review_requires_exact_uncertain_region_coverage(tmp_path: Path) -> None:
    library, project, scene, asset = _library_project(tmp_path)
    workflow = PanelOCRWorkflow(library, _FakeProvider(_result(asset.sha256, 100, 200)))
    extracted = workflow.extract_scene(project.project_id, scene.scene_id)
    task = next(task for task in extracted.review_tasks if task.task_type == "ocr_text_correction")

    with pytest.raises(PanelOCRValidationError, match="exactly uncertain"):
        workflow.apply_corrections(project.project_id, task.review_task_id, {})


def test_panel_ocr_same_raw_extraction_is_idempotent(tmp_path: Path) -> None:
    library, project, scene, asset = _library_project(tmp_path)
    provider = _FakeProvider(_result(asset.sha256, 100, 200))
    workflow = PanelOCRWorkflow(library, provider)
    first = workflow.extract_scene(project.project_id, scene.scene_id)
    first_task = next(task for task in first.review_tasks if task.task_type == "ocr_text_correction")

    second = workflow.extract_scene(project.project_id, scene.scene_id)
    second_task = next(task for task in second.review_tasks if task.task_type == "ocr_text_correction")

    assert second_task.review_task_id == first_task.review_task_id
    assert provider.calls == 1
