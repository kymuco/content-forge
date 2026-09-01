from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.application import (
    ApplicationRepository,
    CharacterRecord,
    DialogueAssignment,
    DialogueAssignmentSuggestion,
    DialogueConflictError,
    DialogueValidationError,
    DialogueWorkflow,
    PanelOCRWorkflow,
    SceneFocusHint,
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
    def __init__(self, *, confidence: float = 0.99) -> None:
        self.confidence = confidence

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
                    raw_text="First provider region",
                    confidence=self.confidence,
                    polygon=(
                        OCRPoint(x=5, y=5),
                        OCRPoint(x=80, y=5),
                        OCRPoint(x=80, y=25),
                        OCRPoint(x=5, y=25),
                    ),
                    bbox=OCRPixelRect(x_min=5, y_min=5, x_max=80, y_max=25),
                ),
                OCRRegion(
                    region_id="ocr_0001",
                    provider_index=1,
                    raw_text="Second provider region",
                    confidence=self.confidence,
                    polygon=(
                        OCRPoint(x=10, y=40),
                        OCRPoint(x=90, y=40),
                        OCRPoint(x=90, y=65),
                        OCRPoint(x=10, y=65),
                    ),
                    bbox=OCRPixelRect(x_min=10, y_min=40, x_max=90, y_max=65),
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


def _project(tmp_path: Path, *, panels: int = 1, confidence: float = 0.99):
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database)
    assets = []
    scenes = []
    for index in range(panels):
        source = tmp_path / f"panel-{index}.bin"
        source.write_bytes(f"dialogue panel {index}".encode())
        ingested = library.assets.ingest_file(
            source,
            media_type=MediaType.IMAGE,
            mime_type="image/png",
        )
        asset = ingested.asset.validated_copy(update={"width": 100, "height": 100})
        repository.enrich_asset(asset)
        assets.append(asset)
        scenes.append(
            Scene(
                order=index,
                duration_seconds=1.0,
                media=AssetRef(asset_id=asset.asset_id),
            )
        )
    project = library.save_project(
        Project(
            content_kind="panel_sequence",
            state=ProjectState.READY,
            source_refs=tuple(AssetRef(asset_id=item.asset_id) for item in assets),
            scenes=tuple(scenes),
        )
    )
    ocr = PanelOCRWorkflow(library, _OCRProvider(confidence=confidence))
    return library, project, tuple(scenes), ocr


def _register_cast(workflow: DialogueWorkflow, project_id: str) -> None:
    workflow.register_character(
        project_id,
        CharacterRecord(
            character_id="alice",
            display_name="Alice",
            aliases=("A",),
        ),
    )
    workflow.register_character(
        project_id,
        CharacterRecord(
            character_id="bob",
            display_name="Bob",
        ),
    )


def test_dialogue_assignment_requires_explicit_reading_order_and_speaker_authority(
    tmp_path: Path,
) -> None:
    library, project, scenes, ocr = _project(tmp_path)
    ocr.extract_scene(project.project_id, scenes[0].scene_id)
    workflow = DialogueWorkflow(library)
    _register_cast(workflow, project.project_id)

    suggestion = DialogueAssignmentSuggestion(
        label="Candidate",
        provider="fake_assistant",
        assignment=DialogueAssignment(
            reading_order=("ocr_0000", "ocr_0001"),
            speaker_by_region={"ocr_0000": "alice", "ocr_0001": "bob"},
        ),
    )
    prepared = workflow.prepare_scene_assignment(
        project.project_id,
        scenes[0].scene_id,
        suggestions=(suggestion,),
    )
    assert prepared.state is ProjectState.NEEDS_REVIEW
    task = next(task for task in prepared.review_tasks if task.task_type == "dialogue_scene_assignment")
    assert task.payload["regions"][0]["region_id"] == "ocr_0000"
    assert task.payload["regions"][1]["region_id"] == "ocr_0001"
    assert {item["character_id"] for item in task.payload["characters"]} == {"alice", "bob"}
    assert len(task.suggestions) == 1
    assert task.accepted_value is None

    accepted = workflow.apply_scene_assignment(
        project.project_id,
        task.review_task_id,
        DialogueAssignment(
            reading_order=("ocr_0001", "ocr_0000"),
            speaker_by_region={"ocr_0000": "alice", "ocr_0001": "bob"},
            focus_hint=SceneFocusHint(mode="speaker"),
        ),
    )
    assert accepted.state is ProjectState.READY
    scene_dialogue = workflow.scene_dialogue(project.project_id, scenes[0].scene_id)
    assert [line.source_region_id for line in scene_dialogue.lines] == ["ocr_0001", "ocr_0000"]
    assert [line.speaker_id for line in scene_dialogue.lines] == ["bob", "alice"]
    assert [line.text for line in scene_dialogue.lines] == [
        "Second provider region",
        "First provider region",
    ]
    assert scene_dialogue.focus_hint == SceneFocusHint(mode="speaker")


def test_dialogue_assignment_rejects_partial_reading_order_and_unknown_speaker(
    tmp_path: Path,
) -> None:
    library, project, scenes, ocr = _project(tmp_path)
    ocr.extract_scene(project.project_id, scenes[0].scene_id)
    workflow = DialogueWorkflow(library)
    _register_cast(workflow, project.project_id)
    prepared = workflow.prepare_scene_assignment(project.project_id, scenes[0].scene_id)
    task = next(task for task in prepared.review_tasks if task.task_type == "dialogue_scene_assignment")

    with pytest.raises(DialogueValidationError, match="reading_order"):
        workflow.apply_scene_assignment(
            project.project_id,
            task.review_task_id,
            DialogueAssignment(
                reading_order=("ocr_0000",),
                speaker_by_region={"ocr_0000": "alice", "ocr_0001": "bob"},
            ),
        )

    with pytest.raises(DialogueValidationError, match="unknown dialogue speaker"):
        workflow.apply_scene_assignment(
            project.project_id,
            task.review_task_id,
            DialogueAssignment(
                reading_order=("ocr_0000", "ocr_0001"),
                speaker_by_region={"ocr_0000": "alice", "ocr_0001": "mallory"},
            ),
        )


def test_dialogue_cannot_start_before_uncertain_ocr_is_corrected(tmp_path: Path) -> None:
    library, project, scenes, ocr = _project(tmp_path, confidence=0.20)
    extracted = ocr.extract_scene(project.project_id, scenes[0].scene_id)
    assert extracted.state is ProjectState.NEEDS_REVIEW
    workflow = DialogueWorkflow(library)
    with pytest.raises(DialogueConflictError, match="another blocking review"):
        workflow.register_character(
            project.project_id,
            CharacterRecord(character_id="alice", display_name="Alice"),
        ) if False else workflow.prepare_scene_assignment(project.project_id, scenes[0].scene_id)


def test_tampered_dialogue_review_payload_cannot_authorize_assignment(tmp_path: Path) -> None:
    library, project, scenes, ocr = _project(tmp_path)
    ocr.extract_scene(project.project_id, scenes[0].scene_id)
    workflow = DialogueWorkflow(library)
    _register_cast(workflow, project.project_id)
    prepared = workflow.prepare_scene_assignment(project.project_id, scenes[0].scene_id)
    task = next(task for task in prepared.review_tasks if task.task_type == "dialogue_scene_assignment")
    payload = task.model_dump(mode="json")["payload"]
    payload["regions"][0]["text"] = "misleading text"
    tampered = task.validated_copy(update={"payload": payload})
    current = library.load_project(project.project_id)
    assert current is not None
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

    with pytest.raises(DialogueConflictError, match="payload no longer matches"):
        workflow.apply_scene_assignment(
            project.project_id,
            task.review_task_id,
            DialogueAssignment(
                reading_order=("ocr_0000", "ocr_0001"),
                speaker_by_region={"ocr_0000": "alice", "ocr_0001": "bob"},
            ),
        )
    assert workflow.manifest(project.project_id).scenes == ()


def test_character_registry_is_frozen_while_assignment_review_is_open(tmp_path: Path) -> None:
    library, project, scenes, ocr = _project(tmp_path)
    ocr.extract_scene(project.project_id, scenes[0].scene_id)
    workflow = DialogueWorkflow(library)
    _register_cast(workflow, project.project_id)
    workflow.prepare_scene_assignment(project.project_id, scenes[0].scene_id)

    with pytest.raises(DialogueConflictError, match="cannot change during open dialogue review"):
        workflow.update_character(
            project.project_id,
            CharacterRecord(character_id="alice", display_name="Alice renamed"),
        )


def test_multi_panel_dialogue_shares_resume_checkpoint_until_last_assignment(
    tmp_path: Path,
) -> None:
    library, project, scenes, ocr = _project(tmp_path, panels=2)
    for scene in scenes:
        ocr.extract_scene(project.project_id, scene.scene_id)
    workflow = DialogueWorkflow(library)
    _register_cast(workflow, project.project_id)

    first = workflow.prepare_scene_assignment(project.project_id, scenes[0].scene_id)
    assert first.state is ProjectState.NEEDS_REVIEW
    second = workflow.prepare_scene_assignment(project.project_id, scenes[1].scene_id)
    assert second.state is ProjectState.NEEDS_REVIEW
    tasks = {
        task.payload["scene_id"]: task
        for task in second.review_tasks
        if task.task_type == "dialogue_scene_assignment" and task.status.value == "open"
    }
    assert set(tasks) == {scenes[0].scene_id, scenes[1].scene_id}

    assignment = DialogueAssignment(
        reading_order=("ocr_0000", "ocr_0001"),
        speaker_by_region={"ocr_0000": "alice", "ocr_0001": "bob"},
    )
    after_first = workflow.apply_scene_assignment(
        project.project_id,
        tasks[scenes[0].scene_id].review_task_id,
        assignment,
    )
    assert after_first.state is ProjectState.NEEDS_REVIEW
    assert after_first.metadata["pr19_dialogue_resume_state"] == ProjectState.READY.value

    after_second = workflow.apply_scene_assignment(
        project.project_id,
        tasks[scenes[1].scene_id].review_task_id,
        assignment,
    )
    assert after_second.state is ProjectState.READY
    assert "pr19_dialogue_resume_state" not in after_second.metadata
    manifest = workflow.manifest(project.project_id)
    assert [item.scene_id for item in manifest.scenes] == [scenes[0].scene_id, scenes[1].scene_id]


def test_focus_hint_contract_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="speaker focus"):
        SceneFocusHint(mode="speaker", face={"x": 0.5, "y": 0.5})
    with pytest.raises(ValueError, match="face focus"):
        SceneFocusHint(mode="face")
    with pytest.raises(ValueError, match="explicit_crop"):
        SceneFocusHint(mode="explicit_crop")
