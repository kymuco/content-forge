"""PR21 project binding and guarded synthesis workflow."""

from __future__ import annotations

from datetime import datetime, timezone

from content_forge.application.dialogue_pr19_integrity import validated_dialogue_manifest
from content_forge.application.tts import (
    LineTTSWorkflow,
    SynthesizedDialogueLine,
    TTSError,
    TTSConflictError,
    tts_manifest,
)
from content_forge.core import Project, ProjectState, dump_json, load_json
from content_forge.core.ids import EntityKind, require_entity_id
from content_forge.providers.tts import TTSProvider
from content_forge.storage import LocalLibrary

from .voice_cast_models import (
    CAST_METADATA_KEY,
    CharacterCastBinding,
    ProjectVoiceCastManifest,
    ResolvedLineVoice,
    VoiceCastConflictError,
    VoiceCastNotFoundError,
    VoiceCastUnavailableError,
    project_metadata_copy,
    voice_cast_manifest,
)
from .voice_cast_registry import VoiceCastRegistry

_CAST_STATES = frozenset({ProjectState.DRAFT, ProjectState.PREPARED, ProjectState.READY})
_PR20_TTS_METADATA_KEY = "pr20_tts"


class _SnapshotGuardedLineTTSWorkflow(LineTTSWorkflow):
    """Make PR21 resolution and PR20 synthesis share one exact Project snapshot."""

    def __init__(self, library: LocalLibrary, provider: TTSProvider, expected_json: str) -> None:
        super().__init__(library, provider)
        self._expected_json = expected_json

    def _snapshot(self, project_id: str):
        project, raw = super()._snapshot(project_id)
        if raw != self._expected_json:
            raise TTSConflictError(f"project changed after voice cast resolution: {project_id}")
        return project, raw


class VoiceCastWorkflow:
    """Bind PR19 characters to reusable cast and feed exact settings into PR20."""

    def __init__(
        self,
        library: LocalLibrary,
        provider: TTSProvider | None = None,
    ) -> None:
        self.library = library
        self.provider = provider
        self.registry = VoiceCastRegistry(library)

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise VoiceCastNotFoundError(f"unknown project: {project_id}")
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
                raise VoiceCastConflictError(f"project changed concurrently: {updated.project_id}")
        return updated

    def _validated_manifest(self, project: Project, dialogue) -> ProjectVoiceCastManifest:
        manifest = voice_cast_manifest(project)
        known_characters = {item.character_id for item in dialogue.characters}
        for binding in manifest.bindings:
            if binding.character_id not in known_characters:
                raise VoiceCastConflictError(
                    "voice cast binding no longer identifies a registered PR19 character"
                )
            revision = self.registry.get(binding.cast_id, binding.cast_revision)
            if revision.definition_sha256 != binding.cast_definition_sha256:
                raise VoiceCastConflictError("voice cast binding revision digest mismatch")
            if binding.settings_override is not None:
                self.registry._validate_reference(
                    binding.settings_override,
                    expected_sha256=binding.settings_override_reference_sha256,
                )
        return manifest

    @staticmethod
    def _invalidate_character_tts(
        project: Project,
        dialogue,
        character_id: str,
        metadata: dict[str, object],
    ) -> None:
        """Remove materialized PR20 receipts whose voice authority just changed.

        Generated audio blobs stay immutable/content-addressed, but their Project receipts
        must not remain visible as current line synthesis after a bind/rebind/unbind. A
        later PR21 synthesis can materialize the line again through the normal PR20 cache
        identity and evidence path.
        """

        try:
            manifest = tts_manifest(project)
        except TTSError as exc:
            raise VoiceCastConflictError(
                "stored PR20 TTS manifest is invalid; refusing voice cast mutation"
            ) from exc
        affected = {
            (scene.scene_id, line.line_id)
            for scene in dialogue.scenes
            for line in scene.lines
            if line.speaker_id == character_id
        }
        if not affected:
            return
        retained = tuple(
            record
            for record in manifest.lines
            if (record.scene_id, record.line_id) not in affected
        )
        if len(retained) == len(manifest.lines):
            return
        metadata[_PR20_TTS_METADATA_KEY] = manifest.validated_copy(
            update={"lines": retained}
        ).model_dump(mode="json")

    def manifest(self, project_id: str) -> ProjectVoiceCastManifest:
        project, _ = self._snapshot(project_id)
        dialogue = validated_dialogue_manifest(project)
        return self._validated_manifest(project, dialogue)

    def bind_character(
        self,
        project_id: str,
        character_id: str,
        cast_id: str,
        *,
        cast_revision: int | None = None,
        settings_override=None,
    ) -> CharacterCastBinding:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _CAST_STATES:
            raise VoiceCastConflictError(
                f"voice cast cannot mutate project in state {project.state.value}"
            )
        dialogue = validated_dialogue_manifest(project)
        current_manifest = self._validated_manifest(project, dialogue)
        if character_id not in {item.character_id for item in dialogue.characters}:
            raise VoiceCastNotFoundError(f"unknown PR19 character: {character_id}")
        revision = self.registry.get(cast_id, cast_revision)
        override_reference_sha256 = None
        if settings_override is not None:
            override_reference_sha256 = self.registry._validate_reference(settings_override)
        binding = CharacterCastBinding(
            character_id=character_id,
            cast_id=revision.cast_id,
            cast_revision=revision.revision,
            cast_definition_sha256=revision.definition_sha256,
            settings_override=settings_override,
            settings_override_reference_sha256=override_reference_sha256,
        )
        existing = next(
            (item for item in current_manifest.bindings if item.character_id == character_id),
            None,
        )
        if existing == binding:
            return binding
        retained = tuple(
            item for item in current_manifest.bindings if item.character_id != character_id
        )
        character_order = {
            item.character_id: index for index, item in enumerate(dialogue.characters)
        }
        bindings = tuple(
            sorted(
                (*retained, binding),
                key=lambda item: character_order[item.character_id],
            )
        )
        updated_manifest = current_manifest.validated_copy(update={"bindings": bindings})
        metadata = project_metadata_copy(project)
        metadata[CAST_METADATA_KEY] = updated_manifest.model_dump(mode="json")
        self._invalidate_character_tts(project, dialogue, character_id, metadata)
        updated = project.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
        self._cas_project(expected_json, updated)
        return binding

    def unbind_character(self, project_id: str, character_id: str) -> ProjectVoiceCastManifest:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _CAST_STATES:
            raise VoiceCastConflictError(
                f"voice cast cannot mutate project in state {project.state.value}"
            )
        dialogue = validated_dialogue_manifest(project)
        current_manifest = self._validated_manifest(project, dialogue)
        bindings = tuple(
            item for item in current_manifest.bindings if item.character_id != character_id
        )
        if len(bindings) == len(current_manifest.bindings):
            raise VoiceCastNotFoundError(f"character has no voice cast binding: {character_id}")
        updated_manifest = current_manifest.validated_copy(update={"bindings": bindings})
        metadata = project_metadata_copy(project)
        metadata[CAST_METADATA_KEY] = updated_manifest.model_dump(mode="json")
        self._invalidate_character_tts(project, dialogue, character_id, metadata)
        updated = project.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
        self._cas_project(expected_json, updated)
        return updated_manifest

    def _resolve_from_project(
        self,
        project: Project,
        scene_id: str,
        line_id: str,
    ) -> ResolvedLineVoice:
        dialogue = validated_dialogue_manifest(project)
        manifest = self._validated_manifest(project, dialogue)
        scene = next((item for item in dialogue.scenes if item.scene_id == scene_id), None)
        if scene is None:
            raise VoiceCastNotFoundError(f"scene has no accepted dialogue: {scene_id}")
        line = next((item for item in scene.lines if item.line_id == line_id), None)
        if line is None:
            raise VoiceCastNotFoundError(f"unknown accepted dialogue line: {line_id}")
        binding = next(
            (item for item in manifest.bindings if item.character_id == line.speaker_id),
            None,
        )
        if binding is None:
            raise VoiceCastNotFoundError(
                f"dialogue character has no voice cast binding: {line.speaker_id}"
            )
        revision = self.registry.get(binding.cast_id, binding.cast_revision)
        if revision.definition_sha256 != binding.cast_definition_sha256:
            raise VoiceCastConflictError("voice cast binding changed after validation")
        override_applied = binding.settings_override is not None
        settings = binding.settings_override or revision.settings
        reference_audio_sha256 = (
            binding.settings_override_reference_sha256
            if override_applied
            else revision.reference_audio_sha256
        )
        return ResolvedLineVoice(
            project_id=project.project_id,
            scene_id=scene.scene_id,
            line_id=line.line_id,
            character_id=line.speaker_id,
            cast_id=revision.cast_id,
            cast_revision=revision.revision,
            cast_definition_sha256=revision.definition_sha256,
            settings=settings,
            reference_audio_sha256=reference_audio_sha256,
            override_applied=override_applied,
        )

    def resolve_line(self, project_id: str, scene_id: str, line_id: str) -> ResolvedLineVoice:
        project, _ = self._snapshot(project_id)
        return self._resolve_from_project(project, scene_id, line_id)

    def _tts_workflow(self, expected_json: str) -> _SnapshotGuardedLineTTSWorkflow:
        if self.provider is None:
            raise VoiceCastUnavailableError("voice cast synthesis requires a configured TTS provider")
        return _SnapshotGuardedLineTTSWorkflow(self.library, self.provider, expected_json)

    def synthesize_line(
        self,
        project_id: str,
        scene_id: str,
        line_id: str,
        *,
        force: bool = False,
    ) -> SynthesizedDialogueLine:
        project, expected_json = self._snapshot(project_id)
        resolved = self._resolve_from_project(project, scene_id, line_id)
        return self._tts_workflow(expected_json).synthesize_line(
            project_id,
            scene_id,
            line_id,
            resolved.settings,
            force=force,
        )

    def preview_character(
        self,
        project_id: str,
        character_id: str,
    ) -> tuple[ResolvedLineVoice, SynthesizedDialogueLine]:
        project, expected_json = self._snapshot(project_id)
        dialogue = validated_dialogue_manifest(project)
        self._validated_manifest(project, dialogue)
        if character_id not in {item.character_id for item in dialogue.characters}:
            raise VoiceCastNotFoundError(f"unknown PR19 character: {character_id}")
        candidate = next(
            (
                (scene.scene_id, line.line_id)
                for scene in dialogue.scenes
                for line in scene.lines
                if line.speaker_id == character_id
            ),
            None,
        )
        if candidate is None:
            raise VoiceCastNotFoundError(
                f"character has no accepted dialogue line to preview: {character_id}"
            )
        scene_id, line_id = candidate
        resolved = self._resolve_from_project(project, scene_id, line_id)
        synthesized = self._tts_workflow(expected_json).synthesize_line(
            project_id,
            scene_id,
            line_id,
            resolved.settings,
        )
        return resolved, synthesized


__all__ = ["VoiceCastWorkflow"]
