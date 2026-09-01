from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from content_forge.application import (
    CharacterCastBinding,
    CharacterRecord,
    DialogueLine,
    LineTTSSettings,
    ProjectDialogueManifest,
    ProjectTTSManifest,
    ProjectVoiceCastManifest,
    SceneDialogue,
    SynthesizedDialogueLine,
    TTSSynthesisError,
    VoicedStoryConflictError,
    VoicedStoryTimingPolicy,
    VoicedStoryWorkflow,
    VoiceCastDefinition,
    scene_dialogue_digest,
)
from content_forge.core import (
    AssetRef,
    MediaType,
    OutputProfile,
    Project,
    ProjectState,
    Scene,
)
from content_forge.providers import (
    TTSInvocationEvidence,
    TTSProviderHealth,
    TTSRequest,
    TTSResult,
    semantic_tts_request_digest,
    tts_cache_key,
)
from content_forge.providers.ocr import OCRPixelRect
from content_forge.storage import LocalLibrary
from content_forge.timeline import compile_timeline


class _RegeneratingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.frames = 12000
        self.health_value = TTSProviderHealth(
            provider_id="fake_tts",
            provider_version="1",
            model_id="synthetic",
            config_sha256="d" * 64,
            available=True,
        )

    def health(self) -> TTSProviderHealth:
        return self.health_value

    def synthesize(self, request: TTSRequest) -> TTSResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic regeneration failure")
        sample = (1000 + self.calls).to_bytes(2, "little", signed=True)
        with wave.open(str(request.output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(sample * self.frames)
        payload = request.output_path.read_bytes()
        return TTSResult(
            audio_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            sample_rate_hz=24000,
            channels=1,
            sample_count=self.frames,
            duration_seconds=self.frames / 24000,
            evidence=TTSInvocationEvidence(
                provider_id=self.health_value.provider_id,
                provider_version=self.health_value.provider_version,
                model_id=self.health_value.model_id,
                model_revision=self.health_value.model_revision,
                engine="fake",
                request_sha256=semantic_tts_request_digest(request),
                config_sha256=self.health_value.config_sha256,
                resolved_voice=request.voice_id,
                resolved_language=request.language,
            ),
        )


def _write_wav(path: Path, *, frames: int, sample: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(sample.to_bytes(2, "little", signed=True) * frames)


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    LocalLibrary,
    VoicedStoryWorkflow,
    _RegeneratingProvider,
    Project,
    dict[str, ProjectDialogueManifest],
    SynthesizedDialogueLine,
]:
    library = LocalLibrary(tmp_path / "runtime")
    panel_path = tmp_path / "panel.bin"
    panel_path.write_bytes(b"pr22 hardening panel")
    panel_asset = library.assets.ingest_file(
        panel_path,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    ).asset
    scene = Scene(
        order=0,
        duration_seconds=2.0,
        media=AssetRef(asset_id=panel_asset.asset_id),
    )
    project = Project(
        content_kind="panel_sequence",
        state=ProjectState.READY,
        source_refs=(AssetRef(asset_id=panel_asset.asset_id),),
        scenes=(scene,),
        output_profiles=(
            OutputProfile(
                profile_id="vertical",
                width=540,
                height=960,
                fps=30.0,
            ),
        ),
    )
    text = "Hello, world!"
    dialogue_scene = SceneDialogue(
        scene_id=scene.scene_id,
        extraction_digest="a" * 64,
        lines=(
            DialogueLine(
                line_id="dlg_ocr_0000",
                order=0,
                source_region_id="ocr_0000",
                text=text,
                speaker_id="alice",
                source_bbox=OCRPixelRect(x_min=1, y_min=1, x_max=20, y_max=20),
            ),
        ),
    )
    dialogue = ProjectDialogueManifest(
        project_id=project.project_id,
        characters=(CharacterRecord(character_id="alice", display_name="Alice"),),
        scenes=(dialogue_scene,),
    )
    authority = {"dialogue": dialogue}
    for target in (
        "content_forge.application.voiced_story.validated_dialogue_manifest",
        "content_forge.application.voice_cast_workflow.validated_dialogue_manifest",
        "content_forge.application.tts.validated_dialogue_manifest",
    ):
        monkeypatch.setattr(target, lambda candidate, authority=authority: authority["dialogue"])

    provider = _RegeneratingProvider()
    workflow = VoicedStoryWorkflow(library, provider)
    settings = LineTTSSettings(voice_id="voice-a", language="en")
    revision = workflow.voice_cast.registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=settings,
        )
    )
    cast_manifest = ProjectVoiceCastManifest(
        project_id=project.project_id,
        bindings=(
            CharacterCastBinding(
                character_id="alice",
                cast_id=revision.cast_id,
                cast_revision=revision.revision,
                cast_definition_sha256=revision.definition_sha256,
            ),
        ),
    )

    audio_path = tmp_path / "initial.wav"
    _write_wav(audio_path, frames=24000, sample=500)
    audio_asset = library.assets.ingest_file(
        audio_path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    semantic_request = TTSRequest(
        output_path=tmp_path / "semantic.wav",
        text=text,
        language=settings.language,
        voice_id=settings.voice_id,
        generation=settings.generation,
    )
    request_digest = semantic_tts_request_digest(semantic_request)
    initial = SynthesizedDialogueLine(
        scene_id=scene.scene_id,
        line_id="dlg_ocr_0000",
        scene_dialogue_digest=scene_dialogue_digest(dialogue_scene),
        source_text=text,
        speaker_id="alice",
        settings=settings,
        cache_key=tts_cache_key(semantic_request, provider.health_value),
        evidence=TTSInvocationEvidence(
            provider_id=provider.health_value.provider_id,
            provider_version=provider.health_value.provider_version,
            model_id=provider.health_value.model_id,
            model_revision=provider.health_value.model_revision,
            engine="fake",
            request_sha256=request_digest,
            config_sha256=provider.health_value.config_sha256,
            resolved_voice=settings.voice_id,
            resolved_language=settings.language,
        ),
        asset_id=audio_asset.asset_id,
        audio_sha256=audio_asset.sha256,
        size_bytes=audio_asset.size_bytes,
        sample_rate_hz=24000,
        channels=1,
        sample_count=24000,
        duration_seconds=1.0,
    )
    metadata = project.model_dump(mode="json")["metadata"]
    metadata["pr20_tts"] = ProjectTTSManifest(
        project_id=project.project_id,
        lines=(initial,),
    ).model_dump(mode="json")
    metadata["pr21_voice_cast"] = cast_manifest.model_dump(mode="json")
    project = project.validated_copy(update={"metadata": metadata})
    library.save_project(project)
    return library, workflow, provider, project, authority, initial


def test_pr22_materialization_reaches_shared_timeline_and_is_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, _, project, authority, initial = _fixture(tmp_path, monkeypatch)

    manifest = workflow.materialize(project.project_id)
    assert manifest is not None
    stored = library.load_project(project.project_id)
    assert stored is not None
    scene = stored.scenes[0]
    assert scene.duration_seconds == 1.3
    assert manifest.scenes[0].base_duration_seconds == 2.0
    assert len(scene.overlays) == len(manifest.scenes[0].lines[0].cues)
    assert all(item.properties.get("pr22_owner") == "pr22_timed_text_v1" for item in scene.overlays)
    assert len(scene.audio_tracks) == 1
    voice = scene.audio_tracks[0]
    assert voice.properties.get("pr22_owner") == "pr22_voice_audio_v1"
    assert voice.asset_ref is not None
    assert voice.asset_ref.asset_id == initial.asset_id
    assert voice.start_seconds == 0.0
    assert voice.duration_seconds == 1.0

    plan = compile_timeline(stored, library.database, profile_id="vertical")
    assert len(plan.overlays) == len(scene.overlays)
    assert len(plan.audio_tracks) == 1
    assert plan.audio_tracks[0].track_type == "voice"
    assert plan.audio_tracks[0].asset_id == initial.asset_id
    assert plan.audio_tracks[0].start_seconds == 0.0
    assert plan.audio_tracks[0].duration_seconds == 1.0

    authority["dialogue"] = ProjectDialogueManifest(
        project_id=project.project_id,
        characters=authority["dialogue"].characters,
        scenes=(),
    )
    assert workflow.materialize(project.project_id) is None
    restored = library.load_project(project.project_id)
    assert restored is not None
    assert restored.scenes[0].duration_seconds == 2.0
    assert not restored.scenes[0].overlays
    assert not restored.scenes[0].audio_tracks
    assert "pr22_voiced_story" not in restored.model_dump(mode="json")["metadata"]


def test_pr22_failed_regeneration_preserves_previous_receipts_and_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, provider, project, _, initial = _fixture(tmp_path, monkeypatch)
    materialized = workflow.materialize(project.project_id)
    assert materialized is not None
    before = library.load_project(project.project_id)
    assert before is not None

    provider.fail = True
    with pytest.raises(TTSSynthesisError, match="TTS provider failed"):
        workflow.regenerate_line(
            project.project_id,
            project.scenes[0].scene_id,
            "dlg_ocr_0000",
        )

    after = library.load_project(project.project_id)
    assert after is not None
    assert after == before
    tts = ProjectTTSManifest.model_validate(
        after.model_dump(mode="json")["metadata"]["pr20_tts"]
    )
    assert tts.lines[0].audio_sha256 == initial.audio_sha256
    assert after.scenes[0].audio_tracks[0].asset_ref is not None
    assert after.scenes[0].audio_tracks[0].asset_ref.asset_id == initial.asset_id


def test_pr22_successful_regeneration_refreshes_audio_timing_and_core_track(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, provider, project, _, initial = _fixture(tmp_path, monkeypatch)
    assert workflow.materialize(project.project_id) is not None

    regenerated, refreshed = workflow.regenerate_line(
        project.project_id,
        project.scenes[0].scene_id,
        "dlg_ocr_0000",
    )

    assert provider.calls == 1
    assert regenerated.cache_key == initial.cache_key
    assert regenerated.audio_sha256 != initial.audio_sha256
    assert regenerated.asset_id != initial.asset_id
    assert regenerated.duration_seconds == 0.5
    assert refreshed is not None
    assert refreshed.scenes[0].duration_seconds == 0.8
    assert refreshed.scenes[0].lines[0].audio_sha256 == regenerated.audio_sha256

    stored = library.load_project(project.project_id)
    assert stored is not None
    scene = stored.scenes[0]
    assert scene.duration_seconds == 0.8
    assert len(scene.audio_tracks) == 1
    assert scene.audio_tracks[0].asset_ref is not None
    assert scene.audio_tracks[0].asset_ref.asset_id == regenerated.asset_id
    assert scene.audio_tracks[0].duration_seconds == 0.5
    assert library.database.get_asset(initial.asset_id) is not None
    assert workflow.manifest(project.project_id) == refreshed


def test_pr22_implicit_refresh_preserves_materialized_custom_timing_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, provider, project, _, _ = _fixture(tmp_path, monkeypatch)
    policy = VoicedStoryTimingPolicy(
        between_line_pause_seconds=0.7,
        scene_tail_seconds=1.1,
    )
    materialized = workflow.materialize(project.project_id, policy=policy)
    assert materialized is not None
    assert materialized.timing_policy == policy
    assert materialized.scenes[0].duration_seconds == 2.1

    preview = workflow.preview(project.project_id)
    assert preview.timing_policy == policy
    assert preview.scenes[0].duration_seconds == 2.1

    before_repeat = library.load_project(project.project_id)
    assert before_repeat is not None
    repeated = workflow.materialize(project.project_id)
    after_repeat = library.load_project(project.project_id)
    assert after_repeat is not None
    assert repeated == materialized
    assert after_repeat.updated_at == before_repeat.updated_at

    regenerated, refreshed = workflow.regenerate_line(
        project.project_id,
        project.scenes[0].scene_id,
        "dlg_ocr_0000",
    )
    assert provider.calls == 1
    assert regenerated.duration_seconds == 0.5
    assert refreshed is not None
    assert refreshed.timing_policy == policy
    assert refreshed.scenes[0].duration_seconds == 1.6


def test_pr22_dematerialize_refuses_to_overwrite_owned_state_after_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, _, project, _, _ = _fixture(tmp_path, monkeypatch)
    assert workflow.materialize(project.project_id) is not None
    stored = library.load_project(project.project_id)
    assert stored is not None

    drifted_scene = stored.scenes[0].validated_copy(update={"duration_seconds": 1.7})
    drifted = stored.validated_copy(update={"scenes": (drifted_scene,)})
    library.save_project(drifted)

    with pytest.raises(VoicedStoryConflictError, match="owned materialization drift"):
        workflow.dematerialize(project.project_id)

    after = library.load_project(project.project_id)
    assert after == drifted
