"""PR21 persistent voice-cast registry and project character bindings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from content_forge.application.dialogue_pr19_integrity import validated_dialogue_manifest
from content_forge.application.tts import LineTTSSettings, LineTTSWorkflow, SynthesizedDialogueLine
from content_forge.core import MediaType, Project, ProjectState, dump_json, load_json
from content_forge.core.ids import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.providers.tts import TTSProvider
from content_forge.storage import LocalLibrary, StorageSchemaError

_CAST_DEFINITION_VERSION = "pr21_voice_cast_definition_v1"
_CAST_REVISION_VERSION = "pr21_voice_cast_revision_v1"
_CAST_MANIFEST_VERSION = "pr21_voice_cast_manifest_v1"
_CAST_METADATA_KEY = "pr21_voice_cast"
_CAST_SCHEMA_COMPONENT = "voice_cast"
_CAST_SCHEMA_VERSION = 1
_MAX_CAST_ENTRIES = 1024
_MAX_PROJECT_BINDINGS = 256
_CAST_STATES = frozenset({ProjectState.DRAFT, ProjectState.PREPARED, ProjectState.READY})


class VoiceCastError(RuntimeError):
    pass


class VoiceCastConflictError(VoiceCastError):
    pass


class VoiceCastNotFoundError(VoiceCastError):
    pass


class VoiceCastValidationError(VoiceCastError):
    pass


class VoiceCastUnavailableError(VoiceCastError):
    pass


def _cast_definition_payload(
    *,
    cast_id: str,
    display_name: str,
    settings: LineTTSSettings,
) -> dict[str, object]:
    return {
        "cast_id": cast_id,
        "display_name": display_name,
        "settings": settings.model_dump(mode="json"),
    }


def _cast_definition_digest(
    *,
    cast_id: str,
    display_name: str,
    settings: LineTTSSettings,
) -> str:
    encoded = json.dumps(
        _cast_definition_payload(
            cast_id=cast_id,
            display_name=display_name,
            settings=settings,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class VoiceCastDefinition(FrozenModel):
    """Reusable semantic voice recipe, independent of narrative character identity."""

    contract_version: Literal["pr21_voice_cast_definition_v1"] = _CAST_DEFINITION_VERSION
    cast_id: RegistryKey
    display_name: str = Field(min_length=1, max_length=512)
    settings: LineTTSSettings

    @field_validator("display_name")
    @classmethod
    def non_blank_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("voice cast display_name must contain non-whitespace content")
        return value


class VoiceCastRevision(FrozenModel):
    """Immutable persisted revision of one reusable cast voice."""

    contract_version: Literal["pr21_voice_cast_revision_v1"] = _CAST_REVISION_VERSION
    cast_id: RegistryKey
    revision: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=512)
    settings: LineTTSSettings
    definition_sha256: SHA256
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("voice cast created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def definition_digest_matches(self):
        expected = _cast_definition_digest(
            cast_id=self.cast_id,
            display_name=self.display_name,
            settings=self.settings,
        )
        if self.definition_sha256 != expected:
            raise ValueError("voice cast revision definition digest mismatch")
        return self


class CharacterCastBinding(FrozenModel):
    """Project-local mapping from PR19 narrative identity to one immutable cast revision."""

    character_id: RegistryKey
    cast_id: RegistryKey
    cast_revision: int = Field(ge=1)
    cast_definition_sha256: SHA256
    settings_override: LineTTSSettings | None = None


class ProjectVoiceCastManifest(FrozenModel):
    contract_version: Literal["pr21_voice_cast_manifest_v1"] = _CAST_MANIFEST_VERSION
    project_id: str
    bindings: tuple[CharacterCastBinding, ...] = Field(
        default=(), max_length=_MAX_PROJECT_BINDINGS
    )

    @model_validator(mode="after")
    def validate_manifest(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        character_ids = tuple(item.character_id for item in self.bindings)
        if len(set(character_ids)) != len(character_ids):
            raise ValueError("voice cast character bindings must be unique")
        return self


class ResolvedLineVoice(FrozenModel):
    """Exact cast evidence and effective PR20 settings for one accepted dialogue line."""

    project_id: str
    scene_id: str
    line_id: str
    character_id: RegistryKey
    cast_id: RegistryKey
    cast_revision: int = Field(ge=1)
    cast_definition_sha256: SHA256
    settings: LineTTSSettings
    override_applied: bool

    @model_validator(mode="after")
    def validate_ids(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        require_entity_id(self.scene_id, EntityKind.SCENE)
        return self


def _plain_metadata(project: Project) -> dict[str, object]:
    metadata = project.model_dump(mode="json")["metadata"]
    if not isinstance(metadata, dict):  # pragma: no cover - core Project contract
        raise VoiceCastValidationError("project metadata is malformed")
    return metadata


def voice_cast_manifest(project: Project) -> ProjectVoiceCastManifest:
    raw = _plain_metadata(project).get(_CAST_METADATA_KEY)
    if raw is None:
        return ProjectVoiceCastManifest(project_id=project.project_id)
    if not isinstance(raw, Mapping):
        raise VoiceCastValidationError("stored PR21 voice cast metadata is malformed")
    try:
        manifest = ProjectVoiceCastManifest.model_validate(raw)
    except Exception as exc:
        raise VoiceCastValidationError("stored PR21 voice cast manifest is malformed") from exc
    if manifest.project_id != project.project_id:
        raise VoiceCastConflictError("voice cast manifest project identity mismatch")
    return manifest


class _VoiceCastRepository:
    """Application-layer SQLite authority for immutable runtime-wide cast revisions."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.database = library.database

    def initialize(self) -> "_VoiceCastRepository":
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM application_schema WHERE component = ?",
                (_CAST_SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > _CAST_SCHEMA_VERSION:
                raise StorageSchemaError(
                    f"voice cast schema {version} is newer than supported {_CAST_SCHEMA_VERSION}"
                )
            if version not in {0, _CAST_SCHEMA_VERSION}:
                raise StorageSchemaError(
                    f"unsupported voice cast schema migration: {version} -> {_CAST_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_cast_revisions (
                    cast_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    reference_asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (cast_id, revision)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voice_cast_revision_created
                ON voice_cast_revisions(cast_id, revision DESC, created_at DESC)
                """
            )
            if version == 0:
                connection.execute(
                    "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                    (_CAST_SCHEMA_COMPONENT, _CAST_SCHEMA_VERSION),
                )
        return self

    @staticmethod
    def _decode(raw: str) -> VoiceCastRevision:
        try:
            return load_json(VoiceCastRevision, raw)
        except Exception as exc:
            raise VoiceCastValidationError("stored voice cast revision is malformed") from exc

    def get(self, cast_id: str, revision: int | None = None) -> VoiceCastRevision | None:
        with self.database.connection() as connection:
            if revision is None:
                row = connection.execute(
                    """
                    SELECT manifest_json FROM voice_cast_revisions
                    WHERE cast_id = ? ORDER BY revision DESC LIMIT 1
                    """,
                    (cast_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT manifest_json FROM voice_cast_revisions
                    WHERE cast_id = ? AND revision = ?
                    """,
                    (cast_id, revision),
                ).fetchone()
        return None if row is None else self._decode(str(row["manifest_json"]))

    def list_latest(self, *, limit: int = _MAX_CAST_ENTRIES) -> tuple[VoiceCastRevision, ...]:
        if limit < 1 or limit > _MAX_CAST_ENTRIES:
            raise VoiceCastValidationError("voice cast list limit is outside allowed range")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT current.manifest_json
                FROM voice_cast_revisions AS current
                WHERE current.revision = (
                    SELECT MAX(candidate.revision)
                    FROM voice_cast_revisions AS candidate
                    WHERE candidate.cast_id = current.cast_id
                )
                ORDER BY current.cast_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._decode(str(row["manifest_json"])) for row in rows)

    def put(self, definition: VoiceCastDefinition) -> VoiceCastRevision:
        digest = _cast_definition_digest(
            cast_id=definition.cast_id,
            display_name=definition.display_name,
            settings=definition.settings,
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT manifest_json FROM voice_cast_revisions
                WHERE cast_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (definition.cast_id,),
            ).fetchone()
            current = None if row is None else self._decode(str(row["manifest_json"]))
            if current is not None and current.definition_sha256 == digest:
                return current
            next_revision = 1 if current is None else current.revision + 1
            revision = VoiceCastRevision(
                cast_id=definition.cast_id,
                revision=next_revision,
                display_name=definition.display_name,
                settings=definition.settings,
                definition_sha256=digest,
                created_at=datetime.now(timezone.utc),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO voice_cast_revisions(
                        cast_id, revision, reference_asset_id, manifest_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        revision.cast_id,
                        revision.revision,
                        revision.settings.reference_asset_id,
                        dump_json(revision),
                        revision.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise VoiceCastConflictError("voice cast revision could not be committed") from exc
        return revision


class VoiceCastRegistry:
    """Runtime-wide reusable cast registry with verified reference-audio authority."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.repository = _VoiceCastRepository(library).initialize()

    def _validate_reference(self, settings: LineTTSSettings) -> None:
        reference_asset_id = settings.reference_asset_id
        if reference_asset_id is None:
            return
        asset = self.library.database.get_asset(reference_asset_id)
        if asset is None:
            raise VoiceCastNotFoundError(
                f"unknown voice cast reference asset: {reference_asset_id}"
            )
        if asset.media_type is not MediaType.AUDIO:
            raise VoiceCastValidationError("voice cast reference asset must be audio")
        try:
            verified = self.library.assets.verify(asset)
        except (FileNotFoundError, OSError) as exc:
            raise VoiceCastConflictError("voice cast reference audio bytes are unavailable") from exc
        if not verified:
            raise VoiceCastConflictError("voice cast reference audio failed content verification")

    def put(self, definition: VoiceCastDefinition) -> VoiceCastRevision:
        self._validate_reference(definition.settings)
        return self.repository.put(definition)

    def get(self, cast_id: str, revision: int | None = None) -> VoiceCastRevision:
        item = self.repository.get(cast_id, revision)
        if item is None:
            suffix = "latest" if revision is None else str(revision)
            raise VoiceCastNotFoundError(f"unknown voice cast revision: {cast_id}@{suffix}")
        self._validate_reference(item.settings)
        return item

    def list_latest(self, *, limit: int = _MAX_CAST_ENTRIES) -> tuple[VoiceCastRevision, ...]:
        items = self.repository.list_latest(limit=limit)
        for item in items:
            self._validate_reference(item.settings)
        return items


class VoiceCastWorkflow:
    """Bind accepted PR19 characters to persistent cast and feed exact settings into PR20."""

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

    def _validate_override(self, settings: LineTTSSettings | None) -> None:
        if settings is not None:
            self.registry._validate_reference(settings)

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
            self._validate_override(binding.settings_override)
        return manifest

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
        settings_override: LineTTSSettings | None = None,
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
        self._validate_override(settings_override)
        binding = CharacterCastBinding(
            character_id=character_id,
            cast_id=revision.cast_id,
            cast_revision=revision.revision,
            cast_definition_sha256=revision.definition_sha256,
            settings_override=settings_override,
        )
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
        metadata = _plain_metadata(project)
        metadata[_CAST_METADATA_KEY] = updated_manifest.model_dump(mode="json")
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
        metadata = _plain_metadata(project)
        metadata[_CAST_METADATA_KEY] = updated_manifest.model_dump(mode="json")
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
        settings = binding.settings_override or revision.settings
        return ResolvedLineVoice(
            project_id=project.project_id,
            scene_id=scene.scene_id,
            line_id=line.line_id,
            character_id=line.speaker_id,
            cast_id=revision.cast_id,
            cast_revision=revision.revision,
            cast_definition_sha256=revision.definition_sha256,
            settings=settings,
            override_applied=binding.settings_override is not None,
        )

    def resolve_line(self, project_id: str, scene_id: str, line_id: str) -> ResolvedLineVoice:
        project, _ = self._snapshot(project_id)
        return self._resolve_from_project(project, scene_id, line_id)

    def synthesize_line(
        self,
        project_id: str,
        scene_id: str,
        line_id: str,
    ) -> SynthesizedDialogueLine:
        if self.provider is None:
            raise VoiceCastUnavailableError("voice cast synthesis requires a configured TTS provider")
        project, expected_json = self._snapshot(project_id)
        resolved = self._resolve_from_project(project, scene_id, line_id)
        return LineTTSWorkflow(self.library, self.provider).synthesize_line(
            project_id,
            scene_id,
            line_id,
            resolved.settings,
            expected_project_json=expected_json,
        )

    def preview_character(
        self,
        project_id: str,
        character_id: str,
    ) -> tuple[ResolvedLineVoice, SynthesizedDialogueLine]:
        if self.provider is None:
            raise VoiceCastUnavailableError("voice preview requires a configured TTS provider")
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
        synthesized = LineTTSWorkflow(self.library, self.provider).synthesize_line(
            project_id,
            scene_id,
            line_id,
            resolved.settings,
            expected_project_json=expected_json,
        )
        return resolved, synthesized


__all__ = [
    "CharacterCastBinding",
    "ProjectVoiceCastManifest",
    "ResolvedLineVoice",
    "VoiceCastConflictError",
    "VoiceCastDefinition",
    "VoiceCastError",
    "VoiceCastNotFoundError",
    "VoiceCastRegistry",
    "VoiceCastRevision",
    "VoiceCastUnavailableError",
    "VoiceCastValidationError",
    "VoiceCastWorkflow",
    "voice_cast_manifest",
]
