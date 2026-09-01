"""PR22 derived voiced-story timing over accepted PR19/PR20/PR21 authority."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from content_forge.application.dialogue import ProjectDialogueManifest, scene_dialogue_digest
from content_forge.application.dialogue_pr19_integrity import validated_dialogue_manifest
from content_forge.application.tts import ProjectTTSManifest, SynthesizedDialogueLine, _wav_metadata, tts_manifest
from content_forge.application.voice_cast_workflow import VoiceCastWorkflow
from content_forge.core import MediaType, Project, ProjectState, dump_json, load_json
from content_forge.core.ids import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.storage import LocalLibrary

_VOICED_STORY_MANIFEST_VERSION = "pr22_voiced_story_manifest_v1"
_VOICED_STORY_SCENE_VERSION = "pr22_voiced_story_scene_v1"
_VOICED_STORY_LINE_VERSION = "pr22_voiced_story_line_v1"
_TIMED_TEXT_CUE_VERSION = "pr22_timed_text_cue_v1"
_TIMING_POLICY_VERSION = "pr22_timing_policy_v1"
_VOICED_STORY_METADATA_KEY = "pr22_voiced_story"
_MAX_SCENES = 10000
_MAX_LINES = 10000
_MAX_CUES_PER_LINE = 256
_EDITABLE_STATES = frozenset({ProjectState.DRAFT, ProjectState.PREPARED, ProjectState.READY})
_PHRASE_END = frozenset(".!?。！？;；…")
_SOFT_BREAK = frozenset(",，:：")


class VoicedStoryError(RuntimeError):
    pass


class VoicedStoryConflictError(VoicedStoryError):
    pass


class VoicedStoryNotFoundError(VoicedStoryError):
    pass


class VoicedStoryNotReadyError(VoicedStoryError):
    pass


class VoicedStoryValidationError(VoicedStoryError):
    pass


class VoicedStoryTimingPolicy(FrozenModel):
    contract_version: Literal["pr22_timing_policy_v1"] = _TIMING_POLICY_VERSION
    between_line_pause_seconds: float = Field(default=0.18, ge=0.0, le=10.0)
    scene_tail_seconds: float = Field(default=0.30, ge=0.0, le=30.0)


class TimedTextCue(FrozenModel):
    contract_version: Literal["pr22_timed_text_cue_v1"] = _TIMED_TEXT_CUE_VERSION
    phrase_index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=30000)
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(gt=0.0)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("timed text cue must have positive duration")
        return self


class VoicedStoryLine(FrozenModel):
    contract_version: Literal["pr22_voiced_story_line_v1"] = _VOICED_STORY_LINE_VERSION
    line_id: str = Field(pattern=r"^dlg_ocr_[0-9]{4}$")
    order: int = Field(ge=0)
    speaker_id: RegistryKey
    source_text: str = Field(min_length=1, max_length=30000)
    cast_id: RegistryKey
    cast_revision: int = Field(ge=1)
    cast_definition_sha256: SHA256
    tts_cache_key: SHA256
    audio_asset_id: str
    audio_sha256: SHA256
    audio_duration_seconds: float = Field(gt=0.0)
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(gt=0.0)
    cues: tuple[TimedTextCue, ...] = Field(min_length=1, max_length=_MAX_CUES_PER_LINE)

    @model_validator(mode="after")
    def validate_line(self):
        require_entity_id(self.audio_asset_id, EntityKind.ASSET)
        if self.end_seconds <= self.start_seconds:
            raise ValueError("voiced story line must have positive duration")
        if abs((self.end_seconds - self.start_seconds) - self.audio_duration_seconds) > 1e-6:
            raise ValueError("voiced story line interval must equal verified audio duration")
        if tuple(cue.phrase_index for cue in self.cues) != tuple(range(len(self.cues))):
            raise ValueError("timed text cue indexes must be contiguous")
        if abs(self.cues[0].start_seconds - self.start_seconds) > 1e-6:
            raise ValueError("first timed text cue must start with its line")
        if abs(self.cues[-1].end_seconds - self.end_seconds) > 1e-6:
            raise ValueError("last timed text cue must end with its line")
        for previous, current in zip(self.cues, self.cues[1:]):
            if abs(previous.end_seconds - current.start_seconds) > 1e-6:
                raise ValueError("timed text cues must be contiguous within a line")
        return self


class VoicedStoryScene(FrozenModel):
    contract_version: Literal["pr22_voiced_story_scene_v1"] = _VOICED_STORY_SCENE_VERSION
    scene_id: str
    scene_dialogue_digest: SHA256
    duration_seconds: float = Field(gt=0.0)
    lines: tuple[VoicedStoryLine, ...] = Field(min_length=1, max_length=_MAX_LINES)

    @model_validator(mode="after")
    def validate_scene(self):
        require_entity_id(self.scene_id, EntityKind.SCENE)
        if tuple(line.order for line in self.lines) != tuple(range(len(self.lines))):
            raise ValueError("voiced story line order must be contiguous")
        if self.duration_seconds + 1e-6 < self.lines[-1].end_seconds:
            raise ValueError("voiced story scene cannot end before dialogue audio")
        return self


class ProjectVoicedStoryManifest(FrozenModel):
    contract_version: Literal["pr22_voiced_story_manifest_v1"] = _VOICED_STORY_MANIFEST_VERSION
    project_id: str
    timing_policy: VoicedStoryTimingPolicy = Field(default_factory=VoicedStoryTimingPolicy)
    scenes: tuple[VoicedStoryScene, ...] = Field(default=(), max_length=_MAX_SCENES)

    @model_validator(mode="after")
    def validate_manifest(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        scene_ids = tuple(scene.scene_id for scene in self.scenes)
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("voiced story scene IDs must be unique")
        return self


def _metadata(project: Project) -> dict[str, object]:
    metadata = project.model_dump(mode="json")["metadata"]
    if not isinstance(metadata, dict):  # pragma: no cover - core Project contract
        raise VoicedStoryValidationError("project metadata is malformed")
    return metadata


def voiced_story_manifest(project: Project) -> ProjectVoicedStoryManifest | None:
    raw = _metadata(project).get(_VOICED_STORY_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise VoicedStoryValidationError("stored PR22 voiced story metadata is malformed")
    try:
        manifest = ProjectVoicedStoryManifest.model_validate(raw)
    except Exception as exc:
        raise VoicedStoryValidationError("stored PR22 voiced story manifest is malformed") from exc
    if manifest.project_id != project.project_id:
        raise VoicedStoryConflictError("voiced story manifest project identity mismatch")
    return manifest


def _microseconds(seconds: float) -> int:
    return max(0, int(round(seconds * 1_000_000.0)))


def _seconds(microseconds: int) -> float:
    return microseconds / 1_000_000.0


def _phrase_chunks(text: str) -> tuple[str, ...]:
    """Deterministic editorial phrases; this deliberately is not forced alignment."""

    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        raise VoicedStoryValidationError("timed text source must contain non-whitespace text")
    phrases: list[str] = []
    current: list[str] = []
    for index, character in enumerate(normalized):
        current.append(character)
        next_character = normalized[index + 1] if index + 1 < len(normalized) else ""
        hard_break = character in _PHRASE_END
        soft_break = character in _SOFT_BREAK and (not next_character or next_character.isspace())
        if hard_break or soft_break:
            phrase = "".join(current).strip()
            if phrase:
                phrases.append(phrase)
            current = []
    tail = "".join(current).strip()
    if tail:
        phrases.append(tail)
    if not phrases:
        phrases = [normalized]
    if len(phrases) > _MAX_CUES_PER_LINE:
        raise VoicedStoryValidationError("timed text phrase count exceeds line budget")
    return tuple(phrases)


def _phrase_weight(text: str) -> int:
    return max(1, sum(1 for character in text if not character.isspace()))


def _timed_cues(text: str, *, line_start_us: int, duration_us: int) -> tuple[TimedTextCue, ...]:
    phrases = _phrase_chunks(text)
    weights = tuple(_phrase_weight(phrase) for phrase in phrases)
    total_weight = sum(weights)
    cues: list[TimedTextCue] = []
    cumulative_weight = 0
    previous_end_us = line_start_us
    line_end_us = line_start_us + duration_us
    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        cumulative_weight += weight
        if index == len(phrases) - 1:
            cue_end_us = line_end_us
        else:
            cue_end_us = line_start_us + int(round(duration_us * cumulative_weight / total_weight))
            cue_end_us = max(previous_end_us + 1, min(cue_end_us, line_end_us - 1))
        cues.append(
            TimedTextCue(
                phrase_index=index,
                text=phrase,
                start_seconds=_seconds(previous_end_us),
                end_seconds=_seconds(cue_end_us),
            )
        )
        previous_end_us = cue_end_us
    return tuple(cues)


class VoicedStoryWorkflow:
    """Materialize deterministic voiced-story timing from exact accepted upstream state."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.voice_cast = VoiceCastWorkflow(library)

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise VoicedStoryNotFoundError(f"unknown project: {project_id}")
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
                raise VoicedStoryConflictError(
                    f"project changed concurrently: {updated.project_id}"
                )
        return updated

    def _validated_tts(
        self,
        project: Project,
        dialogue: ProjectDialogueManifest,
    ) -> ProjectTTSManifest:
        manifest = tts_manifest(project)
        accepted: dict[tuple[str, str], tuple[object, object]] = {}
        for scene in dialogue.scenes:
            for line in scene.lines:
                accepted[(scene.scene_id, line.line_id)] = (scene, line)
        for record in manifest.lines:
            source = accepted.get((record.scene_id, record.line_id))
            if source is None:
                raise VoicedStoryConflictError(
                    "PR20 synthesized line no longer identifies accepted PR19 dialogue"
                )
            scene, line = source
            if record.scene_dialogue_digest != scene_dialogue_digest(scene):
                raise VoicedStoryConflictError(
                    "PR20 synthesized line no longer matches accepted dialogue scene"
                )
            if record.source_text != line.text or record.speaker_id != line.speaker_id:
                raise VoicedStoryConflictError(
                    "PR20 synthesized line no longer matches accepted dialogue line"
                )
            self._validate_audio_record(record)
        return manifest

    def _validate_audio_record(self, record: SynthesizedDialogueLine) -> None:
        asset = self.library.database.get_asset(record.asset_id)
        if asset is None:
            raise VoicedStoryConflictError("PR20 synthesized audio asset metadata is missing")
        if asset.media_type is not MediaType.AUDIO:
            raise VoicedStoryConflictError("PR20 synthesized asset is not audio")
        if asset.sha256 != record.audio_sha256 or asset.size_bytes != record.size_bytes:
            raise VoicedStoryConflictError("PR20 synthesized audio identity no longer matches manifest")
        try:
            if not self.library.assets.verify(asset):
                raise VoicedStoryConflictError("PR20 synthesized audio failed content verification")
            size, sample_rate, channels, frames, duration, digest = _wav_metadata(
                self.library.assets.resolve(asset)
            )
        except (FileNotFoundError, OSError) as exc:
            raise VoicedStoryConflictError("PR20 synthesized audio bytes are unavailable") from exc
        tolerance = max(1e-6, 1.0 / sample_rate)
        if (
            size != record.size_bytes
            or digest != record.audio_sha256
            or sample_rate != record.sample_rate_hz
            or channels != record.channels
            or frames != record.sample_count
            or abs(duration - record.duration_seconds) > tolerance
        ):
            raise VoicedStoryConflictError("PR20 synthesized WAV evidence no longer matches manifest")

    def derive(
        self,
        project: Project,
        *,
        policy: VoicedStoryTimingPolicy | None = None,
    ) -> ProjectVoicedStoryManifest:
        timing_policy = policy or VoicedStoryTimingPolicy()
        dialogue = validated_dialogue_manifest(project)
        if not dialogue.scenes:
            raise VoicedStoryNotReadyError("voiced story requires accepted PR19 dialogue")
        cast_manifest = self.voice_cast._validated_manifest(project, dialogue)
        binding_by_character = {item.character_id: item for item in cast_manifest.bindings}
        tts = self._validated_tts(project, dialogue)
        tts_by_line = {(item.scene_id, item.line_id): item for item in tts.lines}
        pause_us = _microseconds(timing_policy.between_line_pause_seconds)
        tail_us = _microseconds(timing_policy.scene_tail_seconds)
        scenes: list[VoicedStoryScene] = []

        for dialogue_scene in dialogue.scenes:
            cursor_us = 0
            lines: list[VoicedStoryLine] = []
            for index, dialogue_line in enumerate(dialogue_scene.lines):
                binding = binding_by_character.get(dialogue_line.speaker_id)
                if binding is None:
                    raise VoicedStoryNotReadyError(
                        f"dialogue character has no PR21 voice cast binding: {dialogue_line.speaker_id}"
                    )
                revision = self.voice_cast.registry.get(binding.cast_id, binding.cast_revision)
                if revision.definition_sha256 != binding.cast_definition_sha256:
                    raise VoicedStoryConflictError("PR21 cast binding revision digest mismatch")
                effective_settings = binding.settings_override or revision.settings
                record = tts_by_line.get((dialogue_scene.scene_id, dialogue_line.line_id))
                if record is None:
                    raise VoicedStoryNotReadyError(
                        f"accepted dialogue line has no current PR20 synthesis: {dialogue_line.line_id}"
                    )
                if record.settings != effective_settings:
                    raise VoicedStoryConflictError(
                        "PR20 synthesized line settings do not match current PR21 cast authority"
                    )
                duration_us = max(1, _microseconds(record.duration_seconds))
                line_start_us = cursor_us
                line_end_us = line_start_us + duration_us
                lines.append(
                    VoicedStoryLine(
                        line_id=dialogue_line.line_id,
                        order=dialogue_line.order,
                        speaker_id=dialogue_line.speaker_id,
                        source_text=dialogue_line.text,
                        cast_id=binding.cast_id,
                        cast_revision=binding.cast_revision,
                        cast_definition_sha256=binding.cast_definition_sha256,
                        tts_cache_key=record.cache_key,
                        audio_asset_id=record.asset_id,
                        audio_sha256=record.audio_sha256,
                        audio_duration_seconds=_seconds(duration_us),
                        start_seconds=_seconds(line_start_us),
                        end_seconds=_seconds(line_end_us),
                        cues=_timed_cues(
                            dialogue_line.text,
                            line_start_us=line_start_us,
                            duration_us=duration_us,
                        ),
                    )
                )
                cursor_us = line_end_us
                if index + 1 < len(dialogue_scene.lines):
                    cursor_us += pause_us
            cursor_us += tail_us
            scenes.append(
                VoicedStoryScene(
                    scene_id=dialogue_scene.scene_id,
                    scene_dialogue_digest=scene_dialogue_digest(dialogue_scene),
                    duration_seconds=_seconds(max(1, cursor_us)),
                    lines=tuple(lines),
                )
            )
        return ProjectVoicedStoryManifest(
            project_id=project.project_id,
            timing_policy=timing_policy,
            scenes=tuple(scenes),
        )

    def preview(
        self,
        project_id: str,
        *,
        policy: VoicedStoryTimingPolicy | None = None,
    ) -> ProjectVoicedStoryManifest:
        project, _ = self._snapshot(project_id)
        return self.derive(project, policy=policy)

    def manifest(self, project_id: str) -> ProjectVoicedStoryManifest:
        project, _ = self._snapshot(project_id)
        stored = voiced_story_manifest(project)
        if stored is None:
            raise VoicedStoryNotFoundError("project has no materialized PR22 voiced story")
        expected = self.derive(project, policy=stored.timing_policy)
        if stored != expected:
            raise VoicedStoryConflictError(
                "materialized PR22 voiced story no longer matches current upstream authority"
            )
        return stored

    def materialize(
        self,
        project_id: str,
        *,
        policy: VoicedStoryTimingPolicy | None = None,
    ) -> ProjectVoicedStoryManifest:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_STATES:
            raise VoicedStoryConflictError(
                f"voiced story cannot mutate project in state {project.state.value}"
            )
        derived = self.derive(project, policy=policy)
        current = voiced_story_manifest(project)
        if current == derived:
            return current
        metadata = _metadata(project)
        metadata[_VOICED_STORY_METADATA_KEY] = derived.model_dump(mode="json")
        updated = project.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
        self._cas_project(expected_json, updated)
        return derived


__all__ = [
    "ProjectVoicedStoryManifest",
    "TimedTextCue",
    "VoicedStoryConflictError",
    "VoicedStoryError",
    "VoicedStoryLine",
    "VoicedStoryNotFoundError",
    "VoicedStoryNotReadyError",
    "VoicedStoryScene",
    "VoicedStoryTimingPolicy",
    "VoicedStoryValidationError",
    "VoicedStoryWorkflow",
    "voiced_story_manifest",
]
