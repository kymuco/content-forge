from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.application import (
    ApplicationRepository,
    PanelOCRConflictError,
    PanelOCRValidationError,
    PanelOCRWorkflow,
)
from content_forge.core import (
    AssetRef,
    AttentionMode,
    MediaType,
    Project,
    ProjectState,
    ReviewPriority,
    Scene,
)
from content_forge.providers import (
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRRegion,
    OCRRequest,
    OCRResponseError,
    OCRResult,
    PaddleOCRProvider,
    semantic_ocr_request_digest,
)
from content_forge.storage import LocalLibrary


class _Provider:
    def health(self):  # pragma: no cover
        raise AssertionError

    def extract(self, request: OCRRequest) -> OCRResult:
        return OCRResult(
            source_sha256=request.source_sha256,
            width=request.width,
            height=request.height,
            regions=(
                OCRRegion(
                    region_id="ocr_0000",
                    provider_index=0,
                    raw_text="uncertain",
                    confidence=0.20,
                    polygon=(
                        OCRPoint(x=1, y=1),
                        OCRPoint(x=80, y=1),
                        OCRPoint(x=80, y=20),
                        OCRPoint(x=1, y=20),
                    ),
                    bbox=OCRPixelRect(x_min=1, y_min=1, x_max=80, y_max=20),
                ),
            ),
            evidence=OCRInvocationEvidence(
                provider_id="fake",
                provider_version="1",
                model_id="fake",
                request_sha256=semantic_ocr_request_digest(request),
                config_sha256="c" * 64,
            ),
        )


def _review_fixture(tmp_path: Path):
    library = LocalLibrary(tmp_path)
    source = tmp_path / "panel.bin"
    source.write_bytes(b"authority fixture")
    ingested = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    )
    asset = ingested.asset.validated_copy(update={"width": 100, "height": 100})
    ApplicationRepository(library.database).enrich_asset(asset)
    scene = Scene(
        order=0,
        duration_seconds=1.0,
        media=AssetRef(asset_id=asset.asset_id),
    )
    project = library.save_project(
        Project(
            content_kind="panel_sequence",
            state=ProjectState.READY,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
            scenes=(scene,),
        )
    )
    workflow = PanelOCRWorkflow(library, _Provider())
    extracted = workflow.extract_scene(project.project_id, scene.scene_id)
    task = next(task for task in extracted.review_tasks if task.task_type == "ocr_text_correction")
    return library, project, workflow, task


@pytest.mark.parametrize(
    "task_update",
    [
        {"attention": AttentionMode.AUTO},
        {"priority": ReviewPriority.LOW},
        {"blocking": False},
        {"accepted_value": {"unexpected": True}},
    ],
)
def test_ocr_correction_rejects_tampered_review_authority(
    tmp_path: Path,
    task_update: dict[str, object],
) -> None:
    library, project, workflow, task = _review_fixture(tmp_path)
    current = library.load_project(project.project_id)
    assert current is not None
    tampered = task.validated_copy(update=task_update)
    library.save_project(
        current.validated_copy(
            update={
                "review_tasks": tuple(
                    tampered if item.review_task_id == task.review_task_id else item
                    for item in current.review_tasks
                )
            }
        )
    )

    with pytest.raises(PanelOCRValidationError, match="authority is malformed"):
        workflow.apply_corrections(
            project.project_id,
            task.review_task_id,
            {"ocr_0000": "accepted"},
        )
    assert workflow.extraction(project.project_id, current.scenes[0].scene_id).regions[0].corrected_text is None


def test_ocr_correction_rejects_tampered_review_payload(tmp_path: Path) -> None:
    library, project, workflow, task = _review_fixture(tmp_path)
    current = library.load_project(project.project_id)
    assert current is not None
    payload = task.model_dump(mode="json")["payload"]
    assert isinstance(payload, dict)
    regions = payload["regions"]
    assert isinstance(regions, list)
    assert isinstance(regions[0], dict)
    regions[0]["raw_text"] = "misleading reviewer text"
    tampered = task.validated_copy(update={"payload": payload})
    library.save_project(
        current.validated_copy(
            update={
                "review_tasks": tuple(
                    tampered if item.review_task_id == task.review_task_id else item
                    for item in current.review_tasks
                )
            }
        )
    )

    with pytest.raises(PanelOCRConflictError, match="payload no longer matches"):
        workflow.apply_corrections(
            project.project_id,
            task.review_task_id,
            {"ocr_0000": "accepted"},
        )
    assert workflow.extraction(project.project_id, current.scenes[0].scene_id).regions[0].corrected_text is None


class _PaddleResult:
    def __init__(self, payload):
        self.json = {"res": payload}


class _PaddleRuntime:
    def predict(self, _path: str):
        return [
            _PaddleResult(
                {
                    "rec_texts": ["x" * 30001],
                    "rec_scores": [0.9],
                    "rec_polys": [[[1, 1], [20, 1], [20, 10], [1, 10]]],
                    "rec_boxes": [[1, 1, 20, 10]],
                }
            )
        ]


def test_paddle_contract_invalid_region_is_normalized_to_ocr_response_error(
    tmp_path: Path,
) -> None:
    provider = PaddleOCRProvider(
        runtime_factory=lambda _config: _PaddleRuntime(),
        provider_version="3.7.0",
    )
    with pytest.raises(OCRResponseError, match="recognition region violates"):
        provider.extract(
            OCRRequest(
                image_path=tmp_path / "panel.png",
                source_sha256="a" * 64,
                width=100,
                height=100,
            )
        )
