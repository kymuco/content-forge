"""PR19 hardening for dialogue provenance and registry mutation authority."""

from __future__ import annotations

from datetime import datetime, timezone

from content_forge.core import Project, ProjectState, ReviewStatus

from . import dialogue as _base


_original_panel_extraction = _base._panel_extraction


def _panel_extraction(project: Project, scene_id: str):
    """Require retained PR18 OCR to still belong to the scene's current media asset."""

    extraction = _original_panel_extraction(project, scene_id)
    scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
    if scene is None or scene.media is None:
        raise _base.DialogueConflictError(
            "dialogue source scene no longer has its OCR media asset"
        )
    if extraction.asset_id != scene.media.asset_id:
        raise _base.DialogueConflictError(
            "retained OCR extraction no longer matches the dialogue scene media asset"
        )
    return extraction


def _suggestions(values, extraction, manifest):
    """Materialize assisted proposals with stable semantic IDs for idempotent prepare."""

    result = []
    for index, item in enumerate(values):
        _base._validate_assignment(item.assignment, extraction, manifest)
        semantic = {
            "project_id": extraction.project_id,
            "scene_id": extraction.scene_id,
            "index": index,
            "label": item.label,
            "assignment": item.assignment.model_dump(mode="json"),
            "provider": item.provider,
            "metadata": item.model_dump(mode="json")["metadata"],
        }
        encoded = _base.json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        suggestion_id = f"cf_suggestion_{_base.hashlib.sha256(encoded).hexdigest()[:32]}"
        result.append(
            _base.ReviewSuggestion(
                suggestion_id=suggestion_id,
                label=item.label,
                value=item.assignment.model_dump(mode="json"),
                provider=item.provider,
                metadata=item.metadata,
            )
        )
    return tuple(result)


# Base workflow methods resolve these module globals at execution time, so install the
# hardening once without creating a second dialogue engine.
_base._panel_extraction = _panel_extraction
_base._suggestions = _suggestions


class DialogueWorkflow(_base.DialogueWorkflow):
    """Public PR19 workflow with exact-snapshot registry mutation guards."""

    @staticmethod
    def _reject_character_registry_review_collision(project: Project) -> None:
        open_dialogue = tuple(
            task
            for task in project.review_tasks
            if task.task_type == _base._DIALOGUE_REVIEW_TASK
            and task.status is ReviewStatus.OPEN
        )
        if open_dialogue:
            raise _base.DialogueConflictError(
                "character registry cannot change during open dialogue review"
            )
        if project.state is ProjectState.NEEDS_REVIEW:
            blocking = tuple(
                task
                for task in project.review_tasks
                if task.status is ReviewStatus.OPEN and task.blocking
            )
            if blocking:
                raise _base.DialogueConflictError(
                    "character registry cannot change while another blocking review is active"
                )
            raise _base.DialogueConflictError(
                "character registry cannot change while project needs review"
            )

    def register_character(
        self,
        project_id: str,
        character: _base.CharacterRecord,
    ) -> Project:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _base._EDITABLE_DIALOGUE_STATES:
            raise _base.DialogueConflictError(
                f"dialogue cannot mutate project in state {project.state.value}"
            )
        self._reject_character_registry_review_collision(project)
        manifest = _base.dialogue_manifest(project)
        if any(item.character_id == character.character_id for item in manifest.characters):
            raise _base.DialogueConflictError(
                f"character already exists: {character.character_id}"
            )
        updated_manifest = manifest.validated_copy(
            update={"characters": (*manifest.characters, character)}
        )
        metadata = _base._plain_metadata(project)
        metadata[_base._DIALOGUE_METADATA_KEY] = updated_manifest.model_dump(mode="json")
        updated = project.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
        return self._cas_project(expected_json, updated)

    def update_character(
        self,
        project_id: str,
        character: _base.CharacterRecord,
    ) -> Project:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _base._EDITABLE_DIALOGUE_STATES:
            raise _base.DialogueConflictError(
                f"dialogue cannot mutate project in state {project.state.value}"
            )
        self._reject_character_registry_review_collision(project)
        manifest = _base.dialogue_manifest(project)
        if not any(item.character_id == character.character_id for item in manifest.characters):
            raise _base.DialogueNotFoundError(f"unknown character: {character.character_id}")
        characters = tuple(
            character if item.character_id == character.character_id else item
            for item in manifest.characters
        )
        updated_manifest = manifest.validated_copy(update={"characters": characters})
        metadata = _base._plain_metadata(project)
        metadata[_base._DIALOGUE_METADATA_KEY] = updated_manifest.model_dump(mode="json")
        updated = project.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
        return self._cas_project(expected_json, updated)

    def manifest(self, project_id: str) -> _base.ProjectDialogueManifest:
        """Return dialogue only while its retained PR18 provenance is still current."""

        project, _ = self._snapshot(project_id)
        manifest = _base.dialogue_manifest(project)
        for accepted_scene in manifest.scenes:
            extraction = _base._panel_extraction(project, accepted_scene.scene_id)
            if accepted_scene.extraction_digest != _base.panel_extraction_digest(extraction):
                raise _base.DialogueConflictError(
                    "accepted dialogue no longer matches retained OCR extraction"
                )
        return manifest


# Preserve direct-module consumers as well as the package facade.
_base.DialogueWorkflow = DialogueWorkflow


__all__ = ["DialogueWorkflow"]
