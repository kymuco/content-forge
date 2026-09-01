"""Final PR19 integrity binding between accepted dialogue and resolved review evidence."""

from __future__ import annotations

from collections.abc import Mapping

from content_forge.core import Project, ReviewStatus

from . import dialogue as _base
from . import dialogue_pr19_hardening as _hardening


def validated_dialogue_manifest(project: Project) -> _base.ProjectDialogueManifest:
    """Validate accepted PR19 dialogue against one exact Project snapshot."""

    manifest = _base.dialogue_manifest(project)

    for accepted_scene in manifest.scenes:
        extraction = _base._panel_extraction(project, accepted_scene.scene_id)
        if accepted_scene.extraction_digest != _base.panel_extraction_digest(extraction):
            raise _base.DialogueConflictError(
                "accepted dialogue no longer matches retained OCR extraction"
            )

        evidence_tasks = tuple(
            task
            for task in project.review_tasks
            if task.task_type == _base._DIALOGUE_REVIEW_TASK
            and task.payload.get("scene_id") == accepted_scene.scene_id
        )
        if len(evidence_tasks) != 1:
            raise _base.DialogueConflictError(
                "accepted dialogue review evidence is missing or ambiguous"
            )
        task = evidence_tasks[0]
        if (
            task.status is not ReviewStatus.RESOLVED
            or task.resolved_at is None
            or task.attention is not _base.AttentionMode.REVIEW
            or task.priority is not _base.ReviewPriority.HIGH
            or not task.blocking
        ):
            raise _base.DialogueConflictError(
                "accepted dialogue review evidence is malformed"
            )

        accepted_value = task.accepted_value
        if not isinstance(accepted_value, Mapping):
            raise _base.DialogueValidationError(
                "accepted dialogue review value is malformed"
            )
        if set(accepted_value) != {"scene_dialogue_digest", "assignment"}:
            raise _base.DialogueValidationError(
                "accepted dialogue review value is malformed"
            )

        raw_digest = accepted_value.get("scene_dialogue_digest")
        raw_assignment = accepted_value.get("assignment")
        if not isinstance(raw_digest, str):
            raise _base.DialogueValidationError(
                "accepted dialogue review digest is malformed"
            )
        try:
            assignment = _base.DialogueAssignment.model_validate(raw_assignment)
            _base._validate_assignment(assignment, extraction, manifest)
        except Exception as exc:
            raise _base.DialogueValidationError(
                "accepted dialogue assignment evidence is malformed"
            ) from exc

        reconstructed = _base._scene_dialogue_from_assignment(assignment, extraction)
        if reconstructed != accepted_scene:
            raise _base.DialogueConflictError(
                "accepted dialogue no longer matches resolved assignment evidence"
            )
        if raw_digest != _base.scene_dialogue_digest(accepted_scene):
            raise _base.DialogueConflictError(
                "accepted dialogue digest no longer matches resolved review evidence"
            )

    return manifest


class DialogueWorkflow(_hardening.DialogueWorkflow):
    """PR19 workflow with post-acceptance review-evidence verification."""

    def manifest(self, project_id: str) -> _base.ProjectDialogueManifest:
        """Return dialogue only while source and resolved acceptance evidence still agree."""

        project, _ = self._snapshot(project_id)
        return validated_dialogue_manifest(project)


# Preserve direct-module consumers and the package facade after the final hardening layer.
_base.DialogueWorkflow = DialogueWorkflow
_hardening.DialogueWorkflow = DialogueWorkflow


__all__ = ["DialogueWorkflow", "validated_dialogue_manifest"]
