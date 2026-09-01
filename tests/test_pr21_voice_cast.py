from __future__ import annotations

import hashlib
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest

from content_forge.application import (
    ApplicationRepository,
    CharacterRecord,
    DialogueAssignment,
    DialogueWorkflow,
    LineTTSSettings,
    PanelOCRWorkflow,
    TTSConflictError,
    VoiceCastConflictError,
    VoiceCastDefinition,
    VoiceCastRegistry,
    VoiceCastWorkflow,
    tts_manifest,
)
from content_forge.core import AssetRef, MediaType, Project, ProjectState, Scene
from content_forge.providers import (
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRRegion,
    OCRRequest,
    OCRResult,
    TTSInvocationEvidence,
    TTSProviderHealth,
    TTSRequest,
    TTSResult,
    semantic_ocr_request_digest,
    semantic_tts_request_digest,
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
                    raw_text="Voice cast test line",
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


class _TTSProvider:
    def __init__(self) -> None:
        self.calls: list[TTSRequest] = []

    def health(self) -> TTSProviderHealth:
        return TTSProviderHealth(
            provider_id="fake_tts",
            provider_version="1",
            model_id="synthetic",
            config_sha256="d" * 64,
            available=True,
        )

    def synthesize(self, request: TTSRequest) -> TTSResult:
        self.calls.append(request)
        amplitude = {"voice-a": 400, "voice-b": 800, "voice-c": 1200}.get(
            request.voice_id, 200
        )
        with wave.open(str(request.output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            frames = b"".join(
                int(amplitude if index % 2 == 0 else -amplitude).to_bytes(
                    2, byteorder="little", signed=True
                )
                for index in range(480)
            )
            handle.writeframes(frames)
        payload = request.output_path.read_bytes()
        return TTSResult(
            audio_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            sample_rate_hz=24000,
            channels=1,
            sample_count=480,
            duration_seconds=0.02,
            evidence=TTSInvocationEvidence(
                provider_id="fake_tts",
                provider_version="1",
                model_id="synthetic",
                engine="fake",
                request_sha256=semantic_tts_request_digest(request),
                config_sha256="d" * 64,
                resolved_voice=request.voice_id,
                resolved_language=request.language,
            ),
        )


def _accepted_dialogue(
    library: LocalLibrary,
    tmp_path: Path,
    suffix: str,
) -> tuple[str, str]:
    repository = ApplicationRepository(library.database)
    source = tmp_path / f"panel-{suffix}.bin"
    source.write_bytes(f"pr21 panel {suffix}".encode())
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
    dialogue = DialogueWorkflow(library)
    dialogue.register_character(
        project.project_id,
        CharacterRecord(character_id="alice", display_name="Alice"),
    )
    prepared = dialogue.prepare_scene_assignment(project.project_id, scene.scene_id)
    task = next(item for item in prepared.review_tasks if item.task_type == "dialogue_scene_assignment")
    dialogue.apply_scene_assignment(
        project.project_id,
        task.review_task_id,
        DialogueAssignment(
            reading_order=("ocr_0000",),
            speaker_by_region={"ocr_0000": "alice"},
        ),
    )
    return project.project_id, scene.scene_id


def _reference_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 160)


def test_cast_registry_is_revisioned_immutable_and_idempotent(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registry = VoiceCastRegistry(library)
    first_definition = VoiceCastDefinition(
        cast_id="protagonist",
        display_name="Protagonist",
        settings=LineTTSSettings(voice_id="voice-a", language="en"),
    )
    first = registry.put(first_definition)
    repeated = registry.put(first_definition)
    second = registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=LineTTSSettings(voice_id="voice-b", language="en"),
        )
    )

    assert first.revision == 1
    assert repeated == first
    assert second.revision == 2
    assert second.definition_sha256 != first.definition_sha256
    assert registry.get("protagonist", 1) == first
    assert registry.get("protagonist").settings.voice_id == "voice-b"
    assert registry.list_latest() == (second,)


def test_cast_registry_coexists_with_existing_application_schema(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ApplicationRepository(library.database).initialize()
    VoiceCastRegistry(library)
    with library.database.connection() as connection:
        rows = connection.execute(
            "SELECT component, version FROM application_schema ORDER BY component"
        ).fetchall()
    assert [(row["component"], row["version"]) for row in rows] == [
        ("application", 1),
        ("voice_cast", 1),
    ]


def test_cast_reference_audio_is_verified_on_write_and_read(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    path = tmp_path / "reference.wav"
    _reference_wav(path)
    reference = library.assets.ingest_file(
        path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    registry = VoiceCastRegistry(library)
    revision = registry.put(
        VoiceCastDefinition(
            cast_id="clone",
            display_name="Clone",
            settings=LineTTSSettings(
                voice_id="clone-a",
                language="en",
                reference_asset_id=reference.asset_id,
                reference_text="Reference words",
            ),
        )
    )
    assert revision.settings.reference_asset_id == reference.asset_id

    library.assets.resolve(reference).write_bytes(b"corrupt")
    with pytest.raises(VoiceCastConflictError, match="reference audio"):
        registry.get("clone", 1)


def test_project_binding_pins_revision_and_project_override_is_local(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project_id, scene_id = _accepted_dialogue(library, tmp_path, "a")
    workflow = VoiceCastWorkflow(library)
    first = workflow.registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=LineTTSSettings(voice_id="voice-a", language="en"),
        )
    )
    workflow.bind_character(project_id, "alice", "protagonist")
    second = workflow.registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=LineTTSSettings(voice_id="voice-b", language="en"),
        )
    )

    pinned = workflow.resolve_line(project_id, scene_id, "dlg_ocr_0000")
    assert pinned.cast_revision == first.revision
    assert pinned.settings.voice_id == "voice-a"

    workflow.bind_character(
        project_id,
        "alice",
        "protagonist",
        cast_revision=second.revision,
        settings_override=LineTTSSettings(voice_id="voice-c", language="en"),
    )
    resolved = workflow.resolve_line(project_id, scene_id, "dlg_ocr_0000")
    assert resolved.cast_revision == 2
    assert resolved.override_applied is True
    assert resolved.settings.voice_id == "voice-c"
    assert workflow.registry.get("protagonist").settings.voice_id == "voice-b"


def test_same_cast_revision_can_be_reused_across_projects(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    first_project, first_scene = _accepted_dialogue(library, tmp_path, "first")
    second_project, second_scene = _accepted_dialogue(library, tmp_path, "second")
    workflow = VoiceCastWorkflow(library)
    revision = workflow.registry.put(
        VoiceCastDefinition(
            cast_id="narrator",
            display_name="Narrator",
            settings=LineTTSSettings(voice_id="voice-a", language="en"),
        )
    )
    workflow.bind_character(first_project, "alice", "narrator")
    workflow.bind_character(second_project, "alice", "narrator")

    first = workflow.resolve_line(first_project, first_scene, "dlg_ocr_0000")
    second = workflow.resolve_line(second_project, second_scene, "dlg_ocr_0000")
    assert first.cast_definition_sha256 == revision.definition_sha256
    assert second.cast_definition_sha256 == revision.definition_sha256
    assert first.cast_revision == second.cast_revision == 1


def test_cast_synthesis_reuses_pr20_cache_and_rebind_invalidates_it(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project_id, scene_id = _accepted_dialogue(library, tmp_path, "synth")
    provider = _TTSProvider()
    workflow = VoiceCastWorkflow(library, provider)
    workflow.registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=LineTTSSettings(voice_id="voice-a", language="en"),
        )
    )
    workflow.bind_character(project_id, "alice", "protagonist")

    first = workflow.synthesize_line(project_id, scene_id, "dlg_ocr_0000")
    repeated = workflow.synthesize_line(project_id, scene_id, "dlg_ocr_0000")
    assert repeated == first
    assert len(provider.calls) == 1

    revision2 = workflow.registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=LineTTSSettings(voice_id="voice-b", language="en"),
        )
    )
    workflow.bind_character(
        project_id,
        "alice",
        "protagonist",
        cast_revision=revision2.revision,
    )
    rebound_project = library.load_project(project_id)
    assert rebound_project is not None
    assert tts_manifest(rebound_project).lines == ()

    second = workflow.synthesize_line(project_id, scene_id, "dlg_ocr_0000")
    assert len(provider.calls) == 2
    assert second.cache_key != first.cache_key
    assert second.audio_sha256 != first.audio_sha256

    workflow.unbind_character(project_id, "alice")
    unbound_project = library.load_project(project_id)
    assert unbound_project is not None
    assert tts_manifest(unbound_project).lines == ()


def test_cast_synthesis_rejects_project_change_after_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project_id, scene_id = _accepted_dialogue(library, tmp_path, "race")
    provider = _TTSProvider()
    workflow = VoiceCastWorkflow(library, provider)
    workflow.registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=LineTTSSettings(voice_id="voice-a", language="en"),
        )
    )
    workflow.bind_character(project_id, "alice", "protagonist")
    real_factory = workflow._tts_workflow

    def mutate_before_pr20_snapshot(expected_json: str):
        project = library.load_project(project_id)
        assert project is not None
        metadata = project.model_dump(mode="json")["metadata"]
        metadata["synthetic_concurrent_change"] = True
        library.save_project(
            project.validated_copy(
                update={
                    "metadata": metadata,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        )
        return real_factory(expected_json)

    monkeypatch.setattr(workflow, "_tts_workflow", mutate_before_pr20_snapshot)
    with pytest.raises(TTSConflictError, match="after voice cast resolution"):
        workflow.synthesize_line(project_id, scene_id, "dlg_ocr_0000")
    assert provider.calls == []
