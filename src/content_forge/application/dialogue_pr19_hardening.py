"""PR19 hardening for dialogue provenance, review authority, and queue integrity."""

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


def _suggestion_semantic_id(
    item: _base.DialogueAssignmentSuggestion,
    extraction,
    *,
    index: int,
) -> str:
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
    return f"cf_suggestion_{_base.hashlib.sha256(encoded).hexdigest()[:32]}"


def _suggestions(values, extraction, manifest):
    """Materialize assisted proposals with stable semantic IDs for idempotent prepare."""

    result = []
    for index, item in enumerate(values):
        _base._validate_assignment(item.assignment, extraction, manifest)
        result.append(
            _base.ReviewSuggestion(
                suggestion_id=_suggestion_semantic_id(item, extraction, index=index),
                label=item.label,
                value=item.assignment.model_dump(mode="json"),
                provider=item.provider,
                metadata=item.metadata,
            )
        )
    return tuple(result)


def _validate_persisted_suggestions(task, extraction, manifest) -> None:
    """Reject post-creation suggestion tampering before displaying or accepting review."""

    for index, persisted in enumerate(task.suggestions):
        try:
            assignment = _base.DialogueAssignment.model_validate(persisted.value)
            candidate = _base.DialogueAssignmentSuggestion(
                label=persisted.label,
                assignment=assignment,
                provider=persisted.provider,
                metadata=persisted.metadata,
            )
            _base._validate_assignment(assignment, extraction, manifest)
        except Exception as exc:
            raise _base.DialogueValidationError(
                "dialogue assignment suggestion is malformed"
            ) from exc
        if persisted.suggestion_id != _suggestion_semantic_id(
            candidate,
            extraction,
            index=index,
        ):
            raise _base.DialogueConflictError(
                "dialogue assignment suggestion no longer matches its semantic identity"
            )


# Base workflow methods resolve these module globals at execution time, so install the
# hardening once without creating a second dialogue engine.
_base._panel_extraction = _panel_extraction
_base._suggestions = _suggestions


class DialogueWorkflow(_base.DialogueWorkflow):
    """Public PR19 workflow with exact-snapshot review and provenance guards."""

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

    def apply_scene_assignment(
        self,
        project_id: str,
        review_task_id: str,
        assignment: _base.DialogueAssignment,
    ) -> Project:
        """Accept only a canonical task/suggestion snapshot using the same CAS snapshot."""

        _base.require_entity_id(review_task_id, _base.EntityKind.REVIEW)
        project, expected_json = self._snapshot(project_id)
        if project.state is not ProjectState.NEEDS_REVIEW:
            raise _base.DialogueConflictError(
                "open dialogue assignment requires project state needs_review"
            )
        task = next(
            (item for item in project.review_tasks if item.review_task_id == review_task_id),
            None,
        )
        if task is None:
            raise _base.DialogueNotFoundError(f"unknown review task: {review_task_id}")
        if task.task_type != _base._DIALOGUE_REVIEW_TASK:
            raise _base.DialogueValidationError("review task is not a dialogue assignment task")
        if task.status is not ReviewStatus.OPEN or task.resolved_at is not None:
            raise _base.DialogueConflictError("dialogue assignment task is already closed")
        if (
            task.attention is not _base.AttentionMode.REVIEW
            or task.priority is not _base.ReviewPriority.HIGH
            or not task.blocking
            or task.accepted_value is not None
        ):
            raise _base.DialogueValidationError("dialogue assignment task authority is malformed")

        resume_state = _base._validated_resume_state(
            project.metadata.get(_base._DIALOGUE_RESUME_STATE_KEY)
        )
        scene_id = task.payload.get("scene_id")
        if not isinstance(scene_id, str):
            raise _base.DialogueValidationError("dialogue assignment scene identity is malformed")
        try:
            _base.require_entity_id(scene_id, _base.EntityKind.SCENE)
        except ValueError as exc:
            raise _base.DialogueValidationError(
                "dialogue assignment scene identity is malformed"
            ) from exc
        if not any(scene.scene_id == scene_id for scene in project.scenes):
            raise _base.DialogueConflictError("dialogue assignment scene no longer exists")

        manifest = _base.dialogue_manifest(project)
        if any(item.scene_id == scene_id for item in manifest.scenes):
            raise _base.DialogueConflictError("scene already has accepted dialogue")
        extraction = _base._panel_extraction(project, scene_id)
        canonical_payload = _base._review_payload(
            extraction,
            manifest,
            resume_state=resume_state,
        )
        if task.model_dump(mode="json")["payload"] != canonical_payload:
            raise _base.DialogueConflictError(
                "dialogue assignment task payload no longer matches source state"
            )
        _validate_persisted_suggestions(task, extraction, manifest)
        _base._validate_assignment(assignment, extraction, manifest)
        accepted_scene = _base._scene_dialogue_from_assignment(assignment, extraction)

        scene_order = {scene.scene_id: scene.order for scene in project.scenes}
        scenes = tuple(
            sorted(
                (*manifest.scenes, accepted_scene),
                key=lambda item: scene_order[item.scene_id],
            )
        )
        updated_manifest = manifest.validated_copy(update={"scenes": scenes})
        metadata = _base._plain_metadata(project)
        metadata[_base._DIALOGUE_METADATA_KEY] = updated_manifest.model_dump(mode="json")

        now = datetime.now(timezone.utc)
        resolved = task.validated_copy(
            update={
                "status": ReviewStatus.RESOLVED,
                "accepted_value": {
                    "scene_dialogue_digest": _base.scene_dialogue_digest(accepted_scene),
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
            item.status is ReviewStatus.OPEN
            and item.task_type == _base._DIALOGUE_REVIEW_TASK
            for item in tasks
        )
        remaining_blocking = any(
            item.status is ReviewStatus.OPEN and item.blocking for item in tasks
        )
        if not remaining_dialogue:
            metadata.pop(_base._DIALOGUE_RESUME_STATE_KEY, None)
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

    def list_queue(self, *, limit: int = 100) -> dict[str, object]:
        """Return only canonically verifiable open PR19 assignments for the PWA."""

        if limit < 1 or limit > 500:
            raise _base.DialogueValidationError("limit must be between 1 and 500")
        with self.library.database.connection() as connection:
            rows = connection.execute(
                "SELECT project_id, manifest_json FROM projects "
                "ORDER BY updated_at DESC, project_id"
            ).fetchall()

        queue: list[dict[str, object]] = []
        for row in rows:
            try:
                project = _base.load_json(Project, str(row["manifest_json"]))
                if project.project_id != str(row["project_id"]):
                    continue
                if project.state is not ProjectState.NEEDS_REVIEW:
                    continue
                open_tasks = self._open_dialogue_tasks(project)
                if not open_tasks:
                    continue
                scene_ids = [task.payload.get("scene_id") for task in open_tasks]
                if len(scene_ids) != len(set(scene_ids)):
                    continue
                resume_state = _base._validated_resume_state(
                    project.metadata.get(_base._DIALOGUE_RESUME_STATE_KEY)
                )
                manifest = _base.dialogue_manifest(project)
                accepted_scene_ids = {item.scene_id for item in manifest.scenes}

                validated: list[dict[str, object]] = []
                for task in open_tasks:
                    if (
                        task.resolved_at is not None
                        or task.accepted_value is not None
                        or task.attention is not _base.AttentionMode.REVIEW
                        or task.priority is not _base.ReviewPriority.HIGH
                        or not task.blocking
                    ):
                        raise _base.DialogueValidationError(
                            "dialogue assignment task authority is malformed"
                        )
                    scene_id = task.payload.get("scene_id")
                    if not isinstance(scene_id, str) or scene_id in accepted_scene_ids:
                        raise _base.DialogueConflictError(
                            "dialogue assignment task scene lifecycle is malformed"
                        )
                    extraction = _base._panel_extraction(project, scene_id)
                    canonical_payload = _base._review_payload(
                        extraction,
                        manifest,
                        resume_state=resume_state,
                    )
                    if task.model_dump(mode="json")["payload"] != canonical_payload:
                        raise _base.DialogueConflictError(
                            "dialogue assignment task payload no longer matches source state"
                        )
                    _validate_persisted_suggestions(task, extraction, manifest)
                    validated.append(
                        {
                            "project_id": project.project_id,
                            "project_state": project.state.value,
                            "content_kind": str(project.content_kind),
                            "task": task.model_dump(mode="json"),
                        }
                    )
                queue.extend(validated)
            except Exception:
                # One malformed/tampered project is quarantined in place; it cannot poison
                # independent dialogue review work from other projects.
                continue

        queue.sort(
            key=lambda item: (
                item["task"]["created_at"],
                item["project_id"],
                item["task"]["review_task_id"],
            )
        )
        return {"items": queue[:limit]}

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
