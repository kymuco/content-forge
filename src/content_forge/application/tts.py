"""PR20 durable per-line TTS synthesis over accepted PR19 dialogue."""

from __future__ import annotations

import hashlib
import os
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from content_forge.application.dialogue import scene_dialogue_digest
from content_forge.application.dialogue_pr19_integrity import validated_dialogue_manifest
from content_forge.core import MediaType, Project, ProjectState, dump_json, load_json
from content_forge.core.ids import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, LanguageTag, SHA256
from content_forge.providers.tts import (
    TTSGenerationSettings,
    TTSInvocationEvidence,
    TTSProvider,
    TTSProviderError,
    TTSProviderHealth,
    TTSRequest,
    TTSResult,
    TTSVoiceReference,
    semantic_tts_request_digest,
    tts_cache_key,
)
from content_forge.storage import LocalLibrary

_TTS_MANIFEST_VERSION = "pr20_tts_manifest_v1"
_TTS_LINE_VERSION = "pr20_line_synthesis_v1"
_TTS_METADATA_KEY = "pr20_tts"
_MAX_TTS_LINES = 10000
_MAX_TTS_TEXT_CHARS = 4_000_000
_MAX_LINE_AUDIO_BYTES = 512 * 1024 * 1024
_MAX_LINE_DURATION_SECONDS = 30 * 60.0
_TTS_STATES = frozenset({ProjectState.DRAFT, ProjectState.PREPARED, ProjectState.READY})


class TTSError(RuntimeError):
    pass


class TTSConflictError(TTSError):
    pass


class TTSNotFoundError(TTSError):
    pass


class TTSValidationError(TTSError):
    pass


class TTSSynthesisError(TTSError):
    pass


class LineTTSSettings(FrozenModel):
    """Explicit project-local line voice intent. Persistent cast identity remains PR21."""

    voice_id: str = Field(min_length=1, max_length=256)
    language: LanguageTag | None = None
    instruction: str | None = Field(default=None, max_length=4096)
    reference_asset_id: str | None = None
    reference_text: str | None = Field(default=None, max_length=30000)
    x_vector_only_mode: bool = False
    generation: TTSGenerationSettings = Field(default_factory=TTSGenerationSettings)

    @model_validator(mode="after")
    def validate_settings(self):
        if not self.voice_id.strip():
            raise ValueError("line TTS voice_id must contain non-whitespace content")
        if self.instruction is not None and not self.instruction.strip():
            raise ValueError("line TTS instruction must be omitted instead of blank")
        if self.reference_text is not None and not self.reference_text.strip():
            raise ValueError("line TTS reference_text must be omitted instead of blank")
        if self.reference_asset_id is not None:
            require_entity_id(self.reference_asset_id, EntityKind.ASSET)
        elif self.reference_text is not None or self.x_vector_only_mode:
            raise ValueError("reference settings require reference_asset_id")
        return self


class SynthesizedDialogueLine(FrozenModel):
    contract_version: Literal["pr20_line_synthesis_v1"] = _TTS_LINE_VERSION
    scene_id: str
    line_id: str = Field(pattern=r"^dlg_ocr_[0-9]{4}$")
    scene_dialogue_digest: SHA256
    source_text: str = Field(min_length=1, max_length=30000)
    speaker_id: RegistryKey
    settings: LineTTSSettings
    cache_key: SHA256
    evidence: TTSInvocationEvidence
    asset_id: str
    audio_sha256: SHA256
    size_bytes: int = Field(ge=1, le=_MAX_LINE_AUDIO_BYTES)
    sample_rate_hz: int = Field(ge=1, le=768000)
    channels: int = Field(ge=1, le=64)
    sample_count: int = Field(ge=1)
    duration_seconds: float = Field(gt=0.0, le=_MAX_LINE_DURATION_SECONDS)

    @model_validator(mode="after")
    def validate_ids(self):
        require_entity_id(self.scene_id, EntityKind.SCENE)
        require_entity_id(self.asset_id, EntityKind.ASSET)
        return self


class ProjectTTSManifest(FrozenModel):
    contract_version: Literal["pr20_tts_manifest_v1"] = _TTS_MANIFEST_VERSION
    project_id: str
    lines: tuple[SynthesizedDialogueLine, ...] = Field(default=(), max_length=_MAX_TTS_LINES)

    @model_validator(mode="after")
    def validate_manifest(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        keys = tuple((line.scene_id, line.line_id) for line in self.lines)
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate PR20 synthesized dialogue line")
        if sum(len(line.source_text) for line in self.lines) > _MAX_TTS_TEXT_CHARS:
            raise ValueError("PR20 TTS manifest source text exceeds project budget")
        return self


def _plain_metadata(project: Project) -> dict[str, object]:
    metadata = project.model_dump(mode="json")["metadata"]
    if not isinstance(metadata, dict):  # pragma: no cover - Project contract
        raise TTSValidationError("project metadata is malformed")
    return metadata


def tts_manifest(project: Project) -> ProjectTTSManifest:
    raw = _plain_metadata(project).get(_TTS_METADATA_KEY)
    if raw is None:
        return ProjectTTSManifest(project_id=project.project_id)
    try:
        manifest = ProjectTTSManifest.model_validate(raw)
    except Exception as exc:
        raise TTSValidationError("stored PR20 TTS manifest is malformed") from exc
    if manifest.project_id != project.project_id:
        raise TTSConflictError("TTS manifest project identity mismatch")
    return manifest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _wav_metadata(path: Path) -> tuple[int, int, int, int, float, str]:
    if path.is_symlink() or not path.is_file():
        raise TTSValidationError("TTS output is not a regular WAV file")
    size = path.stat().st_size
    if size <= 0 or size > _MAX_LINE_AUDIO_BYTES:
        raise TTSValidationError("TTS WAV size is outside line artifact budget")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            compression = handle.getcomptype()
    except (wave.Error, EOFError, OSError) as exc:
        raise TTSValidationError("TTS output is not a readable WAV file") from exc
    if channels < 1 or sample_rate < 1 or frames < 1:
        raise TTSValidationError("TTS WAV audio geometry is invalid")
    if sample_width != 2 or compression != "NONE":
        raise TTSValidationError("PR20 requires uncompressed PCM16 WAV output")
    duration = frames / sample_rate
    if duration <= 0.0 or duration > _MAX_LINE_DURATION_SECONDS:
        raise TTSValidationError("TTS WAV duration is outside line artifact budget")
    return size, sample_rate, channels, frames, duration, _sha256_file(path)


def _result_matches_output(result: TTSResult, path: Path) -> None:
    size, sample_rate, channels, frames, duration, digest = _wav_metadata(path)
    if result.audio_sha256 != digest or result.size_bytes != size:
        raise TTSValidationError("TTS provider result does not match output bytes")
    if (
        result.sample_rate_hz != sample_rate
        or result.channels != channels
        or result.sample_count != frames
    ):
        raise TTSValidationError("TTS provider result does not match WAV geometry")
    tolerance = max(1e-6, 1.0 / sample_rate)
    if abs(result.duration_seconds - duration) > tolerance:
        raise TTSValidationError("TTS provider result does not match WAV duration")


class LineTTSWorkflow:
    """Generate/cache one verified audio asset per accepted PR19 dialogue line."""

    def __init__(self, library: LocalLibrary, provider: TTSProvider) -> None:
        self.library = library
        self.provider = provider

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise TTSNotFoundError(f"unknown project: {project_id}")
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
                raise TTSConflictError(f"project changed concurrently: {updated.project_id}")
        return updated

    def _dialogue_source(self, project: Project, scene_id: str, line_id: str):
        require_entity_id(scene_id, EntityKind.SCENE)
        manifest = validated_dialogue_manifest(project)
        scene = next((item for item in manifest.scenes if item.scene_id == scene_id), None)
        if scene is None:
            raise TTSNotFoundError(f"scene has no accepted dialogue: {scene_id}")
        line = next((item for item in scene.lines if item.line_id == line_id), None)
        if line is None:
            raise TTSNotFoundError(f"unknown accepted dialogue line: {line_id}")
        return manifest, scene, line

    def _request(
        self,
        *,
        line,
        settings: LineTTSSettings,
        output_path: Path,
    ) -> TTSRequest:
        reference = None
        if settings.reference_asset_id is not None:
            asset = self.library.database.get_asset(settings.reference_asset_id)
            if asset is None:
                raise TTSNotFoundError(
                    f"unknown TTS reference asset: {settings.reference_asset_id}"
                )
            if asset.media_type is not MediaType.AUDIO:
                raise TTSValidationError("TTS reference asset must be audio")
            try:
                verified = self.library.assets.verify(asset)
            except (FileNotFoundError, OSError) as exc:
                raise TTSConflictError("TTS reference asset bytes are unavailable") from exc
            if not verified:
                raise TTSConflictError("TTS reference asset failed content verification")
            reference = TTSVoiceReference(
                audio_path=self.library.assets.resolve(asset),
                audio_sha256=asset.sha256,
                text=settings.reference_text,
                x_vector_only_mode=settings.x_vector_only_mode,
            )
        return TTSRequest(
            output_path=output_path,
            text=line.text,
            language=settings.language,
            voice_id=settings.voice_id,
            instruction=settings.instruction,
            reference=reference,
            generation=settings.generation,
        )

    @staticmethod
    def _health_identity(health: TTSProviderHealth) -> TTSProviderHealth:
        if not health.available:
            raise TTSSynthesisError(health.reason or "TTS provider is unavailable")
        return health

    def _provider_health(self) -> TTSProviderHealth:
        try:
            return self._health_identity(self.provider.health())
        except TTSError:
            raise
        except Exception as exc:
            raise TTSSynthesisError(
                f"TTS provider health check failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _validate_evidence(
        result: TTSResult,
        request: TTSRequest,
        health: TTSProviderHealth,
    ) -> None:
        evidence = result.evidence
        if evidence.request_sha256 != semantic_tts_request_digest(request):
            raise TTSValidationError("TTS provider returned mismatched request evidence")
        if (
            evidence.provider_id != health.provider_id
            or evidence.provider_version != health.provider_version
            or evidence.model_id != health.model_id
            or evidence.model_revision != health.model_revision
            or evidence.config_sha256 != health.config_sha256
        ):
            raise TTSValidationError("TTS provider identity changed during synthesis")

    def _validate_record_identity(
        self,
        record: SynthesizedDialogueLine,
        *,
        scene,
        line,
    ) -> None:
        if record.scene_dialogue_digest != scene_dialogue_digest(scene):
            raise TTSConflictError("cached TTS line no longer matches accepted dialogue scene")
        if record.source_text != line.text or record.speaker_id != line.speaker_id:
            raise TTSConflictError("cached TTS line no longer matches accepted dialogue line")
        request = self._request(
            line=line,
            settings=record.settings,
            output_path=self.library.paths.incoming / "pr20-semantic-cache.wav",
        )
        if record.evidence.request_sha256 != semantic_tts_request_digest(request):
            raise TTSConflictError("cached TTS request evidence no longer matches source state")
        health = TTSProviderHealth(
            provider_id=record.evidence.provider_id,
            provider_version=record.evidence.provider_version,
            model_id=record.evidence.model_id,
            model_revision=record.evidence.model_revision,
            config_sha256=record.evidence.config_sha256,
            available=True,
        )
        if record.cache_key != tts_cache_key(request, health):
            raise TTSConflictError("cached TTS semantic identity is malformed")
        asset = self.library.database.get_asset(record.asset_id)
        if asset is None:
            raise TTSConflictError("cached TTS asset metadata is missing")
        if asset.media_type is not MediaType.AUDIO:
            raise TTSConflictError("cached TTS asset is not catalogued as audio")
        if asset.sha256 != record.audio_sha256 or asset.size_bytes != record.size_bytes:
            raise TTSConflictError("cached TTS asset identity no longer matches manifest")

    def _validate_cached_asset(self, record: SynthesizedDialogueLine) -> None:
        asset = self.library.database.get_asset(record.asset_id)
        if asset is None:  # identity validation normally catches this first
            raise TTSConflictError("cached TTS asset metadata is missing")
        try:
            if not self.library.assets.verify(asset):
                raise TTSConflictError("cached TTS asset failed content verification")
            path = self.library.assets.resolve(asset)
            size, sample_rate, channels, frames, duration, digest = _wav_metadata(path)
        except (FileNotFoundError, OSError) as exc:
            raise TTSConflictError("cached TTS asset bytes are unavailable") from exc
        tolerance = max(1e-6, 1.0 / sample_rate)
        if (
            size != record.size_bytes
            or digest != record.audio_sha256
            or sample_rate != record.sample_rate_hz
            or channels != record.channels
            or frames != record.sample_count
            or abs(duration - record.duration_seconds) > tolerance
        ):
            raise TTSConflictError("cached TTS WAV evidence no longer matches manifest")

    def _validated_manifest_identity(self, project: Project, dialogue) -> ProjectTTSManifest:
        manifest = tts_manifest(project)
        scene_by_id = {scene.scene_id: scene for scene in dialogue.scenes}
        for record in manifest.lines:
            scene = scene_by_id.get(record.scene_id)
            if scene is None:
                raise TTSConflictError("cached TTS scene no longer has accepted dialogue")
            line = next((item for item in scene.lines if item.line_id == record.line_id), None)
            if line is None:
                raise TTSConflictError("cached TTS source line no longer exists")
            self._validate_record_identity(record, scene=scene, line=line)
        return manifest

    def manifest(self, project_id: str) -> ProjectTTSManifest:
        project, _ = self._snapshot(project_id)
        dialogue = validated_dialogue_manifest(project)
        manifest = self._validated_manifest_identity(project, dialogue)
        for record in manifest.lines:
            self._validate_cached_asset(record)
        return manifest

    def synthesize_line(
        self,
        project_id: str,
        scene_id: str,
        line_id: str,
        settings: LineTTSSettings,
        *,
        force: bool = False,
    ) -> SynthesizedDialogueLine:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _TTS_STATES:
            raise TTSConflictError(
                f"TTS cannot mutate project in state {project.state.value}"
            )
        dialogue, scene, line = self._dialogue_source(project, scene_id, line_id)
        current_manifest = self._validated_manifest_identity(project, dialogue)
        current = next(
            (
                item
                for item in current_manifest.lines
                if item.scene_id == scene_id and item.line_id == line_id
            ),
            None,
        )

        health = self._provider_health()
        semantic_request = self._request(
            line=line,
            settings=settings,
            output_path=self.library.paths.incoming / "pr20-semantic-request.wav",
        )
        expected_cache_key = tts_cache_key(semantic_request, health)
        if current is not None:
            self._validate_cached_asset(current)
            if current.cache_key == expected_cache_key and not force:
                return current

        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.library.paths.incoming,
            prefix="pr20-tts-",
            suffix=".wav",
        )
        os.close(descriptor)
        output_path = Path(temporary_name)
        output_path.unlink(missing_ok=True)
        try:
            request = self._request(line=line, settings=settings, output_path=output_path)
            if semantic_tts_request_digest(request) != semantic_tts_request_digest(semantic_request):
                raise TTSValidationError("TTS runtime path changed semantic request identity")
            try:
                result = self.provider.synthesize(request)
            except TTSProviderError as exc:
                raise TTSSynthesisError(f"TTS provider failed: {type(exc).__name__}") from exc
            except TTSError:
                raise
            except Exception as exc:
                raise TTSSynthesisError(f"TTS provider failed: {type(exc).__name__}") from exc
            self._validate_evidence(result, request, health)
            _result_matches_output(result, output_path)
            ingested = self.library.assets.ingest_file(
                output_path,
                media_type=MediaType.AUDIO,
                mime_type="audio/wav",
            )
            asset = ingested.asset
            if asset.media_type is not MediaType.AUDIO:
                raise TTSConflictError("generated TTS bytes collide with a non-audio asset")
            if asset.sha256 != result.audio_sha256 or asset.size_bytes != result.size_bytes:
                raise TTSConflictError("ingested TTS asset identity does not match provider output")

            accepted = SynthesizedDialogueLine(
                scene_id=scene.scene_id,
                line_id=line.line_id,
                scene_dialogue_digest=scene_dialogue_digest(scene),
                source_text=line.text,
                speaker_id=line.speaker_id,
                settings=settings,
                cache_key=expected_cache_key,
                evidence=result.evidence,
                asset_id=asset.asset_id,
                audio_sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                sample_rate_hz=result.sample_rate_hz,
                channels=result.channels,
                sample_count=result.sample_count,
                duration_seconds=result.duration_seconds,
            )

            retained = tuple(
                item
                for item in current_manifest.lines
                if not (item.scene_id == scene_id and item.line_id == line_id)
            )
            scene_order = {item.scene_id: item.order for item in project.scenes}
            line_order = {
                (dialogue_scene.scene_id, dialogue_line.line_id): dialogue_line.order
                for dialogue_scene in dialogue.scenes
                for dialogue_line in dialogue_scene.lines
            }
            lines = tuple(
                sorted(
                    (*retained, accepted),
                    key=lambda item: (
                        scene_order[item.scene_id],
                        line_order[(item.scene_id, item.line_id)],
                    ),
                )
            )
            updated_manifest = current_manifest.validated_copy(update={"lines": lines})
            metadata = _plain_metadata(project)
            metadata[_TTS_METADATA_KEY] = updated_manifest.model_dump(mode="json")
            updated = project.validated_copy(
                update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
            )
            self._cas_project(expected_json, updated)
            return accepted
        finally:
            output_path.unlink(missing_ok=True)


__all__ = [
    "LineTTSSettings",
    "LineTTSWorkflow",
    "ProjectTTSManifest",
    "SynthesizedDialogueLine",
    "TTSConflictError",
    "TTSError",
    "TTSNotFoundError",
    "TTSSynthesisError",
    "TTSValidationError",
    "tts_manifest",
]
