"""PR19 dialogue scene and speaker-assignment workflow over retained PR18 OCR."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from content_forge.application.panel_ocr import PanelTextExtraction, panel_extraction_digest
from content_forge.core import (
    AttentionMode,
    NormalizedPoint,
    NormalizedRect,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
    ReviewSuggestion,
    ReviewTask,
    dump_json,
    load_json,
)
from content_forge.core.ids import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel
from content_forge.providers.ocr import OCRPixelRect
from content_forge.storage import LocalLibrary

_DIALOGUE_MANIFEST_VERSION = "pr19_dialogue_manifest_v1"
_DIALOGUE_SCENE_VERSION = "pr19_scene_dialogue_v1"
_DIALOGUE_REVIEW_VERSION = "pr19_dialogue_assignment_review_v1"
_DIALOGUE_METADATA_KEY = "pr19_dialogue"
_DIALOGUE_RESUME_STATE_KEY = "pr19_dialogue_resume_state"
_DIALOGUE_REVIEW_TASK = "dialogue_scene_assignment"
_PR18_OCR_METADATA_KEY = "pr18_panel_ocr"
_MAX_CHARACTERS = 256
_MAX_ALIASES_PER_CHARACTER = 64
_MAX_DIALOGUE_REGIONS = 2048
_MAX_DIALOGUE_TEXT_CHARS = 1_000_000
_EDITABLE_DIALOGUE_STATES = frozenset(
    {
        ProjectState.INBOX,
        ProjectState.DRAFT,
        ProjectState.PREPARED,
        ProjectState.NEEDS_REVIEW,
        ProjectState.READY,
    }
)


class DialogueError(RuntimeError):
    pass


class DialogueConflictError(DialogueError):
    pass


class DialogueNotFoundError(DialogueError):
    pass


class DialogueValidationError(DialogueError):
    pass


class CharacterRecord(FrozenModel):
    """Project-local narrative identity. Voice/cast identity remains PR21."""

    character_id: RegistryKey
    display_name: str = Field(min_length=1, max_length=512)
    aliases: tuple[str, ...] = Field(default=(), max_length=_MAX_ALIASES_PER_CHARACTER)
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_aliases(self) -> Self:
        normalized = tuple(alias.casefold() for alias in self.aliases)
        if any(not alias.strip() for alias in self.aliases):
            raise ValueError("character aliases must be non-empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("character aliases must be unique")
        return self


class SceneFocusHint(FrozenModel):
    """Semantic camera intent; PR23 owns concrete choreography."""

    mode: Literal["speaker", "face", "explicit_crop"]
    face: NormalizedPoint | None = None
    crop: NormalizedRect | None = None

    @model_validator(mode="after")
    def validate_mode_payload(self) -> Self:
        if self.mode == "speaker":
            if self.face is not None or self.crop is not None:
                raise ValueError("speaker focus does not accept explicit geometry")
        elif self.mode == "face":
            if self.face is None or self.crop is not None:
                raise ValueError("face focus requires exactly one normalized face point")
        elif self.mode == "explicit_crop":
            if self.crop is None or self.face is not None:
                raise ValueError("explicit_crop focus requires exactly one normalized crop")
        return self


class DialogueAssignment(FrozenModel):
    """Human-acceptance payload for one panel scene."""

    reading_order: tuple[str, ...] = Field(min_length=1, max_length=_MAX_DIALOGUE_REGIONS)
    speaker_by_region: Mapping[str, RegistryKey]
    focus_hint: SceneFocusHint | None = None


class DialogueAssignmentSuggestion(FrozenModel):
    """Optional proposal only; never grants speaker-assignment authority."""

    label: str = Field(min_length=1, max_length=4096)
    assignment: DialogueAssignment
    provider: str | None = Field(default=None, max_length=512)
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)


class DialogueLine(FrozenModel):
    line_id: str = Field(pattern=r"^dlg_ocr_[0-9]{4}$")
    order: int = Field(ge=0)
    source_region_id: str = Field(pattern=r"^ocr_[0-9]{4}$")
    text: str = Field(min_length=1, max_length=30000)
    speaker_id: RegistryKey
    source_bbox: OCRPixelRect


class SceneDialogue(FrozenModel):
    contract_version: Literal["pr19_scene_dialogue_v1"] = _DIALOGUE_SCENE_VERSION
    scene_id: str
    extraction_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lines: tuple[DialogueLine, ...] = Field(min_length=1, max_length=_MAX_DIALOGUE_REGIONS)
    focus_hint: SceneFocusHint | None = None

    @model_validator(mode="after")
    def validate_scene_dialogue(self) -> Self:
        require_entity_id(self.scene_id, EntityKind.SCENE)
        line_ids = tuple(line.line_id for line in self.lines)
        region_ids = tuple(line.source_region_id for line in self.lines)
        orders = tuple(line.order for line in self.lines)
        if len(set(line_ids)) != len(line_ids):
            raise ValueError("dialogue line IDs must be unique")
        if len(set(region_ids)) != len(region_ids):
            raise ValueError("dialogue source region IDs must be unique")
        if orders != tuple(range(len(self.lines))):
            raise ValueError("dialogue line order must be contiguous and canonical")
        if sum(len(line.text) for line in self.lines) > _MAX_DIALOGUE_TEXT_CHARS:
            raise ValueError("dialogue text exceeds scene budget")
        return self


class ProjectDialogueManifest(FrozenModel):
    contract_version: Literal["pr19_dialogue_manifest_v1"] = _DIALOGUE_MANIFEST_VERSION
    project_id: str
    characters: tuple[CharacterRecord, ...] = Field(default=(), max_length=_MAX_CHARACTERS)
    scenes: tuple[SceneDialogue, ...] = ()

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        require_entity_id(self.project_id, EntityKind.PROJECT)
        character_ids = tuple(item.character_id for item in self.characters)
        scene_ids = tuple(item.scene_id for item in self.scenes)
        if len(set(character_ids)) != len(character_ids):
            raise ValueError("dialogue character IDs must be unique")
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("dialogue scene IDs must be unique")
        known = set(character_ids)
        for scene in self.scenes:
            for line in scene.lines:
                if line.speaker_id not in known:
                    raise ValueError("dialogue line speaker_id must identify a registered character")
        return self


def scene_dialogue_digest(scene: SceneDialogue) -> str:
    encoded = json.dumps(
        scene.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plain_metadata(project: Project) -> dict[str, object]:
    metadata = project.model_dump(mode="json")["metadata"]
    if not isinstance(metadata, dict):  # pragma: no cover - core Project contract
        raise DialogueValidationError("project metadata is malformed")
    return metadata


def dialogue_manifest(project: Project) -> ProjectDialogueManifest:
    raw = _plain_metadata(project).get(_DIALOGUE_METADATA_KEY)
    if raw is None:
        return ProjectDialogueManifest(project_id=project.project_id)
    if not isinstance(raw, Mapping):
        raise DialogueValidationError("project PR19 dialogue metadata is malformed")
    try:
        manifest = ProjectDialogueManifest.model_validate(raw)
    except Exception as exc:
        raise DialogueValidationError("stored PR19 dialogue manifest is malformed") from exc
    if manifest.project_id != project.project_id:
        raise DialogueConflictError("dialogue manifest project identity mismatch")
    return manifest


def _panel_extraction(project: Project, scene_id: str) -> PanelTextExtraction:
    raw_root = _plain_metadata(project).get(_PR18_OCR_METADATA_KEY)
    if not isinstance(raw_root, Mapping):
        raise DialogueNotFoundError("scene has no retained PR18 OCR extraction")
    raw = raw_root.get(scene_id)
    if not isinstance(raw, Mapping):
        raise DialogueNotFoundError(f"scene has no retained PR18 OCR extraction: {scene_id}")
    try:
        extraction = PanelTextExtraction.model_validate(raw)
    except Exception as exc:
        raise DialogueValidationError("retained PR18 OCR extraction is malformed") from exc
    if extraction.project_id != project.project_id or extraction.scene_id != scene_id:
        raise DialogueConflictError("retained OCR extraction identity does not match dialogue scene")
    if extraction.uncertain_region_ids:
        raise DialogueConflictError("dialogue assignment requires all OCR uncertainty to be resolved")
    if not extraction.regions:
        raise DialogueValidationError("dialogue assignment requires at least one OCR text region")
    return extraction


def _validated_resume_state(value: object) -> ProjectState:
    try:
        state = ProjectState(value)
    except (TypeError, ValueError) as exc:
        raise DialogueValidationError("project dialogue resume state is malformed") from exc
    if state not in _EDITABLE_DIALOGUE_STATES or state is ProjectState.NEEDS_REVIEW:
        raise DialogueValidationError("project dialogue resume state is not resumable")
    return state


def _character_payload(character: CharacterRecord) -> dict[str, object]:
    return {
        "character_id": character.character_id,
        "display_name": character.display_name,
        "aliases": list(character.aliases),
    }


def _review_payload(
    extraction: PanelTextExtraction,
    manifest: ProjectDialogueManifest,
    *,
    resume_state: ProjectState,
) -> dict[str, object]:
    if len(extraction.regions) > _MAX_DIALOGUE_REGIONS:
        raise DialogueValidationError("dialogue region count exceeds review budget")
    if sum(len(region.effective_text) for region in extraction.regions) > _MAX_DIALOGUE_TEXT_CHARS:
        raise DialogueValidationError("dialogue source text exceeds review budget")
    return {
        "contract_version": _DIALOGUE_REVIEW_VERSION,
        "scene_id": extraction.scene_id,
        "asset_id": extraction.asset_id,
        "resume_state": resume_state.value,
        "extraction_digest": panel_extraction_digest(extraction),
        "regions": [
            {
                "region_id": region.region_id,
                "text": region.effective_text,
                "bbox": region.bbox.model_dump(mode="json"),
            }
            for region in extraction.regions
        ],
        "characters": [_character_payload(character) for character in manifest.characters],
        "focus_modes": ["speaker", "face", "explicit_crop"],
    }


def _validate_assignment(
    assignment: DialogueAssignment,
    extraction: PanelTextExtraction,
    manifest: ProjectDialogueManifest,
) -> None:
    source_ids = tuple(region.region_id for region in extraction.regions)
    requested = tuple(assignment.reading_order)
    if len(set(requested)) != len(requested) or set(requested) != set(source_ids):
        raise DialogueValidationError("reading_order must cover each OCR region exactly once")
    speaker_map = dict(assignment.speaker_by_region)
    if set(speaker_map) != set(source_ids):
        raise DialogueValidationError("speaker assignment must cover each OCR region exactly once")
    known_characters = {item.character_id for item in manifest.characters}
    unknown = sorted(set(speaker_map.values()) - known_characters)
    if unknown:
        raise DialogueValidationError(f"unknown dialogue speaker IDs: {unknown}")


def _suggestions(
    values: tuple[DialogueAssignmentSuggestion, ...],
    extraction: PanelTextExtraction,
    manifest: ProjectDialogueManifest,
) -> tuple[ReviewSuggestion, ...]:
    result: list[ReviewSuggestion] = []
    for item in values:
        _validate_assignment(item.assignment, extraction, manifest)
        result.append(
            ReviewSuggestion(
                label=item.label,
                value=item.assignment.model_dump(mode="json"),
                provider=item.provider,
                metadata=item.metadata,
            )
        )
    return tuple(result)


def _scene_dialogue_from_assignment(
    assignment: DialogueAssignment,
    extraction: PanelTextExtraction,
) -> SceneDialogue:
    by_id = {region.region_id: region for region in extraction.regions}
    speaker_map = dict(assignment.speaker_by_region)
    return SceneDialogue(
        scene_id=extraction.scene_id,
        extraction_digest=panel_extraction_digest(extraction),
        lines=tuple(
            DialogueLine(
                line_id=f"dlg_{region_id}",
                order=order,
                source_region_id=region_id,
                text=by_id[region_id].effective_text,
                speaker_id=speaker_map[region_id],
                source_bbox=by_id[region_id].bbox,
            )
            for order, region_id in enumerate(assignment.reading_order)
        ),
        focus_hint=assignment.focus_hint,
    )


class DialogueWorkflow:
    """Durable PR19 character, reading-order, speaker, and focus authority."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise DialogueNotFoundError(f"unknown project: {project_id}")
        raw = str(row["manifest_json"])
        return load_json(Project, raw), raw

    def _cas_project(self, expected_json: str, updated: Project) -> Project:
        serialized = dump_json(updated)
        with self.library.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE projects
                SET content_kind = ?, state = ?, manifest_json = ?, updated_at = ?
                WHERE project_id = ? AND manifest_json = ?
                """,
                (
                    updated.content_kind,
                    updated.state.value,
                    serialized,
                    updated.updated_at.isoformat(),
                    updated.project_id,
                    expected_json,
                ),
            ).rowcount
            if changed != 1:
                raise DialogueConflictError(f"project changed concurrently: {updated.project_id}")
        return updated

    @staticmethod
    def _open_dialogue_tasks(project: Project) -> tuple[ReviewTask, ...]:
        return tuple(
            task
            for task in project.review_tasks
            if task.task_type == _DIALOGUE_REVIEW_TASK and task.status is ReviewStatus.OPEN
        )

    def _resume_state(self, project: Project) -> ProjectState:
        raw = project.metadata.get(_DIALOGUE_RESUME_STATE_KEY)
        if raw is not None:
            if project.state is not ProjectState.NEEDS_REVIEW:
                raise DialogueConflictError("dialogue resume checkpoint exists outside needs_review")
            return _validated_resume_state(raw)
        if project.state is ProjectState.NEEDS_REVIEW:
            blockers = [
                task
                for task in project.review_tasks
                if task.status is ReviewStatus.OPEN and task.blocking
            ]
            if blockers:
                raise DialogueConflictError(
                    "dialogue review cannot start while another blocking review is active"
                )
            raise DialogueConflictError("needs_review project has no dialogue resume checkpoint")
        if project.state not in _EDITABLE_DIALOGUE_STATES:
            raise DialogueConflictError(
                f"dialogue cannot mutate project in state {project.state.value}"
            )
        return project.state

    def register_character(
        self,
        project_id: str,
        character: CharacterRecord,
    ) -> Project:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_DIALOGUE_STATES:
            raise DialogueConflictError(
                f"dialogue cannot mutate project in state {project.state.value}"
            )
        if self._open_dialogue_tasks(project):
            raise DialogueConflictError("character registry cannot change during open dialogue review")
        manifest = dialogue_manifest(project)
        if any(item.character_id == character.character_id for item in manifest.characters):
            raise DialogueConflictError(f"character already exists: {character.character_id}")
        updated_manifest = manifest.validated_copy(
            update={"characters": (*manifest.characters, character)}
        )
        metadata = _plain_metadata(project)
        metadata[_DIALOGUE_METADATA_KEY] = updated_manifest.model_dump(mode="json")
        updated = project.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
        return self._cas_project(expected_json, updated)

    def update_character(
        self,
        project_id: str,
        character: CharacterRecord,
    ) -> Project:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_DIALOGUE_STATES:
            raise DialogueConflictError(
                f"dialogue cannot mutate project in state {project.state.value}"
            )
        if self._open_dialogue_tasks(project):
            raise DialogueConflictError("character registry cannot change during open dialogue review")
        manifest = dialogue_manifest(project)
        if not any(item.character_id == character.character_id for item in manifest.characters):
            raise DialogueNotFoundError(f"unknown character: {character.character_id}")
        characters = tuple(
            character if item.character_id == character.character_id else item
            for item in manifest.characters
        )
        updated_manifest = manifest.validated_copy(update={"characters": characters})
        metadata = _plain_metadata(project)
        metadata[_DIALOGUE_METADATA_KEY] = updated_manifest.model_dump(mode="json")
        updated = project.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
        return self._cas_project(expected_json, updated)

    def prepare_scene_assignment(
        self,
        project_id: str,
        scene_id: str,
        *,
        suggestions: tuple[DialogueAssignmentSuggestion, ...] = (),
    ) -> Project:
        require_entity_id(scene_id, EntityKind.SCENE)
        project, expected_json = self._snapshot(project_id)
        scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
        if scene is None:
            raise DialogueNotFoundError(f"unknown project scene: {scene_id}")
        manifest = dialogue_manifest(project)
        if not manifest.characters:
            raise DialogueValidationError("dialogue assignment requires at least one registered character")
        if any(item.scene_id == scene_id for item in manifest.scenes):
            raise DialogueConflictError("scene already has accepted dialogue; explicit migration is required")
        extraction = _panel_extraction(project, scene_id)
        resume_state = self._resume_state(project)
        canonical_payload = _review_payload(extraction, manifest, resume_state=resume_state)
        canonical_suggestions = _suggestions(suggestions, extraction, manifest)

        existing = [
            task
            for task in project.review_tasks
            if task.task_type == _DIALOGUE_REVIEW_TASK
            and task.payload.get("scene_id") == scene_id
        ]
        if existing:
            if len(existing) != 1:
                raise DialogueConflictError("scene has duplicate dialogue assignment tasks")
            task = existing[0]
            if (
                task.status is ReviewStatus.OPEN
                and task.attention is AttentionMode.REVIEW
                and task.priority is ReviewPriority.HIGH
                and task.blocking
                and task.model_dump(mode="json")["payload"] == canonical_payload
                and task.suggestions == canonical_suggestions
            ):
                return project
            raise DialogueConflictError("scene already has non-canonical dialogue assignment state")

        review_task = ReviewTask(
            project_id=project.project_id,
            task_type=_DIALOGUE_REVIEW_TASK,
            attention=AttentionMode.REVIEW,
            priority=ReviewPriority.HIGH,
            blocking=True,
            payload=canonical_payload,
            suggestions=canonical_suggestions,
        )
        metadata = _plain_metadata(project)
        checkpoint = metadata.get(_DIALOGUE_RESUME_STATE_KEY)
        if checkpoint is not None and checkpoint != resume_state.value:
            raise DialogueConflictError("dialogue resume checkpoint changed unexpectedly")
        metadata[_DIALOGUE_RESUME_STATE_KEY] = resume_state.value
        updated = project.validated_copy(
            update={
                "state": ProjectState.NEEDS_REVIEW,
                "metadata": metadata,
                "review_tasks": (*project.review_tasks, review_task),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self._cas_project(expected_json, updated)

    def apply_scene_assignment(
        self,
        project_id: str,
        review_task_id: str,
        assignment: DialogueAssignment,
    ) -> Project:
        require_entity_id(review_task_id, EntityKind.REVIEW)
        project, expected_json = self._snapshot(project_id)
        if project.state is not ProjectState.NEEDS_REVIEW:
            raise DialogueConflictError("open dialogue assignment requires project state needs_review")
        task = next(
            (item for item in project.review_tasks if item.review_task_id == review_task_id),
            None,
        )
        if task is None:
            raise DialogueNotFoundError(f"unknown review task: {review_task_id}")
        if task.task_type != _DIALOGUE_REVIEW_TASK:
            raise DialogueValidationError("review task is not a dialogue assignment task")
        if task.status is not ReviewStatus.OPEN or task.resolved_at is not None:
            raise DialogueConflictError("dialogue assignment task is already closed")
        if (
            task.attention is not AttentionMode.REVIEW
            or task.priority is not ReviewPriority.HIGH
            or not task.blocking
            or task.accepted_value is not None
        ):
            raise DialogueValidationError("dialogue assignment task authority is malformed")

        resume_state = _validated_resume_state(project.metadata.get(_DIALOGUE_RESUME_STATE_KEY))
        scene_id = task.payload.get("scene_id")
        if not isinstance(scene_id, str):
            raise DialogueValidationError("dialogue assignment scene identity is malformed")
        try:
            require_entity_id(scene_id, EntityKind.SCENE)
        except ValueError as exc:
            raise DialogueValidationError("dialogue assignment scene identity is malformed") from exc
        if not any(scene.scene_id == scene_id for scene in project.scenes):
            raise DialogueConflictError("dialogue assignment scene no longer exists")

        manifest = dialogue_manifest(project)
        if any(item.scene_id == scene_id for item in manifest.scenes):
            raise DialogueConflictError("scene already has accepted dialogue")
        extraction = _panel_extraction(project, scene_id)
        canonical_payload = _review_payload(extraction, manifest, resume_state=resume_state)
        if task.model_dump(mode="json")["payload"] != canonical_payload:
            raise DialogueConflictError("dialogue assignment task payload no longer matches source state")
        _validate_assignment(assignment, extraction, manifest)
        accepted_scene = _scene_dialogue_from_assignment(assignment, extraction)

        scene_order = {scene.scene_id: scene.order for scene in project.scenes}
        scenes = tuple(
            sorted(
                (*manifest.scenes, accepted_scene),
                key=lambda item: scene_order[item.scene_id],
            )
        )
        updated_manifest = manifest.validated_copy(update={"scenes": scenes})
        metadata = _plain_metadata(project)
        metadata[_DIALOGUE_METADATA_KEY] = updated_manifest.model_dump(mode="json")

        now = datetime.now(timezone.utc)
        resolved = task.validated_copy(
            update={
                "status": ReviewStatus.RESOLVED,
                "accepted_value": {
                    "scene_dialogue_digest": scene_dialogue_digest(accepted_scene),
                    "assignment": assignment.model_dump(mode="json"),
                },
                "resolved_at": now,
            }
        )
        tasks = tuple(
            resolved if item.review_task_id == task.review_task_id else item
            for item in project.review_tasks
        )
        remaining_dialogue = any(
            item.status is ReviewStatus.OPEN and item.task_type == _DIALOGUE_REVIEW_TASK
            for item in tasks
        )
        remaining_blocking = any(
            item.status is ReviewStatus.OPEN and item.blocking for item in tasks
        )
        if not remaining_dialogue:
            metadata.pop(_DIALOGUE_RESUME_STATE_KEY, None)
        next_state = ProjectState.NEEDS_REVIEW if remaining_blocking else resume_state
        updated = project.validated_copy(
            update={
                "state": next_state,
                "metadata": metadata,
                "review_tasks": tasks,
                "updated_at": now,
            }
        )
        return self._cas_project(expected_json, updated)

    def manifest(self, project_id: str) -> ProjectDialogueManifest:
        project, _ = self._snapshot(project_id)
        return dialogue_manifest(project)

    def scene_dialogue(self, project_id: str, scene_id: str) -> SceneDialogue:
        require_entity_id(scene_id, EntityKind.SCENE)
        manifest = self.manifest(project_id)
        result = next((scene for scene in manifest.scenes if scene.scene_id == scene_id), None)
        if result is None:
            raise DialogueNotFoundError(f"scene has no accepted dialogue: {scene_id}")
        return result


__all__ = [
    "CharacterRecord",
    "DialogueAssignment",
    "DialogueAssignmentSuggestion",
    "DialogueConflictError",
    "DialogueError",
    "DialogueLine",
    "DialogueNotFoundError",
    "DialogueValidationError",
    "DialogueWorkflow",
    "ProjectDialogueManifest",
    "SceneDialogue",
    "SceneFocusHint",
    "dialogue_manifest",
    "scene_dialogue_digest",
]
