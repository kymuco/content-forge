from __future__ import annotations

import hashlib
import wave
from datetime import datetime, timezone
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
    VoicedStoryConflictError,
    VoicedStoryNotFoundError,
    VoicedStoryNotReadyError,
    VoicedStoryTimingPolicy,
    VoicedStoryWorkflow,
    VoiceCastDefinition,
    scene_dialogue_digest,
)
from content_forge.core import AssetRef, MediaType, Project, ProjectState, Scene
from content_forge.providers import TTSInvocationEvidence
from content_forge.providers.ocr import OCRPixelRect
from content_forge.storage import LocalLibrary


def _wav(path: Path, *, frames: int = 24000, sample_rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str = "Hello, world! Another phrase.",
    settings: LineTTSSettings | None = None,
) -> tuple[LocalLibrary, VoicedStoryWorkflow, Project, ProjectDialogueManifest]:
    library = LocalLibrary(tmp_path / "runtime")
    panel_path = tmp_path / "panel.bin"
    panel_path.write_bytes(b"pr22 panel")
    panel_asset = library.assets.ingest_file(
        panel_path,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    ).asset
    scene = Scene(
        order=0,
        duration_seconds=1.0,
        media=AssetRef(asset_id=panel_asset.asset_id),
    )
    project = Project(
        content_kind="panel_sequence",
        state=ProjectState.READY,
        source_refs=(AssetRef(asset_id=panel_asset.asset_id),),
        scenes=(scene,),
    )
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
    monkeypatch.setattr(
        "content_forge.application.voiced_story.validated_dialogue_manifest",
        lambda candidate: dialogue,
    )

    workflow = VoicedStoryWorkflow(library)
    cast_settings = settings or LineTTSSettings(voice_id="voice-a", language="en")
    revision = workflow.voice_cast.registry.put(
        VoiceCastDefinition(
            cast_id="protagonist",
            display_name="Protagonist",
            settings=cast_settings,
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

    audio_path = tmp_path / "line.wav"
    _wav(audio_path)
    audio_asset = library.assets.ingest_file(
        audio_path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    line = SynthesizedDialogueLine(
        scene_id=scene.scene_id,
        line_id="dlg_ocr_0000",
        scene_dialogue_digest=scene_dialogue_digest(dialogue_scene),
        source_text=text,
        speaker_id="alice",
        settings=cast_settings,
        cache_key="b" * 64,
        evidence=TTSInvocationEvidence(
            provider_id="fake_tts",
            provider_version="1",
            model_id="synthetic",
            engine="fake",
            request_sha256="c" * 64,
            config_sha256="d" * 64,
            resolved_voice=cast_settings.voice_id,
            resolved_language=cast_settings.language,
        ),
        asset_id=audio_asset.asset_id,
        audio_sha256=audio_asset.sha256,
        size_bytes=audio_asset.size_bytes,
        sample_rate_hz=24000,
        channels=1,
        sample_count=24000,
        duration_seconds=1.0,
    )
    tts = ProjectTTSManifest(project_id=project.project_id, lines=(line,))
    metadata = project.model_dump(mode="json")["metadata"]
    metadata["pr20_tts"] = tts.model_dump(mode="json")
    metadata["pr21_voice_cast"] = cast_manifest.model_dump(mode="json")
    project = project.validated_copy(update={"metadata": metadata})
    library.save_project(project)
    return library, workflow, project, dialogue


def test_pr22_derives_deterministic_phrase_timing_and_scene_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, project, _ = _fixture(tmp_path, monkeypatch)
    policy = VoicedStoryTimingPolicy(
        between_line_pause_seconds=0.2,
        scene_tail_seconds=0.3,
    )

    first = workflow.preview(project.project_id, policy=policy)
    second = workflow.preview(project.project_id, policy=policy)

    assert first == second
    assert first.scenes[0].duration_seconds == 1.3
    persisted = library.load_project(project.project_id)
    assert persisted is not None
    assert persisted.scenes[0].duration_seconds == 1.0
    line = first.scenes[0].lines[0]
    assert line.start_seconds == 0.0
    assert line.end_seconds == 1.0
    assert tuple(cue.text for cue in line.cues) == (
        "Hello,",
        "world!",
        "Another phrase.",
    )
    assert line.cues[0].start_seconds == 0.0
    assert line.cues[-1].end_seconds == 1.0
    assert all(
        left.end_seconds == right.start_seconds
        for left, right in zip(line.cues, line.cues[1:])
    )


def test_pr22_materialization_is_idempotent_and_revalidates_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, project, _ = _fixture(tmp_path, monkeypatch)
    materialized = workflow.materialize(project.project_id)
    raw_after_first = library.load_project(project.project_id)
    assert raw_after_first is not None
    assert raw_after_first.scenes[0].duration_seconds == materialized.scenes[0].duration_seconds
    assert raw_after_first.scenes[0].duration_seconds == 1.3
    updated_at = raw_after_first.updated_at

    repeated = workflow.materialize(project.project_id)
    raw_after_repeat = library.load_project(project.project_id)
    assert raw_after_repeat is not None
    assert repeated == materialized
    assert raw_after_repeat.updated_at == updated_at
    assert workflow.manifest(project.project_id) == materialized

    metadata = raw_after_repeat.model_dump(mode="json")["metadata"]
    tts = ProjectTTSManifest.model_validate(metadata["pr20_tts"])
    changed = tts.lines[0].validated_copy(update={"cache_key": "e" * 64})
    metadata["pr20_tts"] = tts.validated_copy(update={"lines": (changed,)}).model_dump(
        mode="json"
    )
    library.save_project(
        raw_after_repeat.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
    )
    with pytest.raises(VoicedStoryConflictError, match="no longer matches current upstream"):
        workflow.manifest(project.project_id)


def test_pr22_materialized_manifest_rejects_core_scene_duration_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, project, _ = _fixture(tmp_path, monkeypatch)
    workflow.materialize(project.project_id)
    stored = library.load_project(project.project_id)
    assert stored is not None
    changed_scene = stored.scenes[0].validated_copy(update={"duration_seconds": 9.0})
    library.save_project(
        stored.validated_copy(
            update={
                "scenes": (changed_scene,),
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )

    with pytest.raises(VoicedStoryConflictError, match="no longer matches current upstream"):
        workflow.manifest(project.project_id)


def test_pr22_requires_current_synthesis_for_every_accepted_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, project, _ = _fixture(tmp_path, monkeypatch)
    stored = library.load_project(project.project_id)
    assert stored is not None
    metadata = stored.model_dump(mode="json")["metadata"]
    metadata["pr20_tts"] = ProjectTTSManifest(project_id=project.project_id).model_dump(
        mode="json"
    )
    library.save_project(stored.validated_copy(update={"metadata": metadata}))

    with pytest.raises(VoicedStoryNotReadyError, match="no current PR20 synthesis"):
        workflow.preview(project.project_id)


def test_pr22_rejects_audio_that_does_not_match_current_cast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, project, _ = _fixture(tmp_path, monkeypatch)
    stored = library.load_project(project.project_id)
    assert stored is not None
    metadata = stored.model_dump(mode="json")["metadata"]
    tts = ProjectTTSManifest.model_validate(metadata["pr20_tts"])
    changed = tts.lines[0].validated_copy(
        update={"settings": LineTTSSettings(voice_id="other-voice", language="en")}
    )
    metadata["pr20_tts"] = tts.validated_copy(update={"lines": (changed,)}).model_dump(
        mode="json"
    )
    library.save_project(stored.validated_copy(update={"metadata": metadata}))

    with pytest.raises(VoicedStoryConflictError, match="current PR21 cast authority"):
        workflow.preview(project.project_id)


def test_pr22_manifest_requires_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workflow, project, _ = _fixture(tmp_path, monkeypatch)
    with pytest.raises(VoicedStoryNotFoundError, match="no materialized PR22"):
        workflow.manifest(project.project_id)
