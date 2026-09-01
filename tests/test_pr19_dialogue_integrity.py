from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.application import (
    ApplicationRepository,
    CharacterRecord,
    DialogueAssignment,
    DialogueConflictError,
    DialogueWorkflow,
    PanelOCRWorkflow,
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


class _OCRProvider:
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
                    raw_text="Accepted line",
                    confidence=0.99,
                    polygon=(
                        OCRPoint(x=5, y=5),
                        OCRPoint(x=80, y=5),
                        OCRPoint(x=80, y=25),
                        OCRPoint(x=5, y=25),
                    ),
                    bbox=OCRPixelRect(x_min=5, y_min=5, x_max=80, y_max=25),
                ),
            ),
            evidence=OCRInvocationEvidence(
                provider_id="fake",
                provider_version="1",
                model_id="synthetic",
                request_sha256=semantic_ocr_request_digest(request),
                config_sha256="c" * 64,
            ),
        )


def _accepted_dialogue(tmp_path: Path):
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database)
    source = tmp_path / "dialogue-integrity-panel.bin"
    source.write_bytes(b"pr19 dialogue integrity panel")
    ingested = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    )
    asset = ingested.asset.validated_copy(update={"width": 100, "height": 100})
    repository.enrich_asset(asset)
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
    PanelOCRWorkflow(library, _OCRProvider()).extract_scene(project.project_id, scene.scene_id)

    workflow = DialogueWorkflow(library)
    workflow.register_character(
        project.project_id,
        CharacterRecord(character_id="alice", display_name="Alice"),
    )
    workflow.register_character(
        project.project_id,
        CharacterRecord(character_id="bob", display_name="Bob"),
    )
    prepared = workflow.prepare_scene_assignment(project.project_id, scene.scene_id)
    task = next(
        item for item in prepared.review_tasks if item.task_type == "dialogue_scene_assignment"
    )
    workflow.apply_scene_assignment(
        project.project_id,
        task.review_task_id,
        DialogueAssignment(
            reading_order=("ocr_0000",),
            speaker_by_region={"ocr_0000": "alice"},
        ),
    )
    return library, workflow, project.project_id


def test_accepted_dialogue_manifest_cannot_drift_from_resolved_assignment(tmp_path: Path) -> None:
    library, workflow, project_id = _accepted_dialogue(tmp_path)
    current = library.load_project(project_id)
    assert current is not None

    metadata = current.model_dump(mode="json")["metadata"]
    metadata["pr19_dialogue"]["scenes"][0]["lines"][0]["speaker_id"] = "bob"
    library.save_project(current.validated_copy(update={"metadata": metadata}))

    with pytest.raises(DialogueConflictError, match="resolved assignment evidence"):
        workflow.manifest(project_id)


def test_accepted_dialogue_digest_cannot_drift_from_resolved_review(tmp_path: Path) -> None:
    library, workflow, project_id = _accepted_dialogue(tmp_path)
    current = library.load_project(project_id)
    assert current is not None

    task = next(
        item
        for item in current.review_tasks
        if item.task_type == "dialogue_scene_assignment"
    )
    accepted_value = task.model_dump(mode="json")["accepted_value"]
    accepted_value["scene_dialogue_digest"] = "0" * 64
    changed_task = task.validated_copy(update={"accepted_value": accepted_value})
    library.save_project(
        current.validated_copy(
            update={
                "review_tasks": tuple(
                    changed_task if item.review_task_id == task.review_task_id else item
                    for item in current.review_tasks
                )
            }
        )
    )

    with pytest.raises(DialogueConflictError, match="digest no longer matches"):
        workflow.manifest(project_id)
