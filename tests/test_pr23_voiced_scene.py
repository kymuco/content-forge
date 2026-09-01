from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.application import (
    CharacterRecord,
    DialogueLine,
    ProjectDialogueManifest,
    ProjectVoicedStoryManifest,
    SceneDialogue,
    SceneFocusHint,
    TimedTextCue,
    VoicedSceneConflictError,
    VoicedSceneWorkflow,
    VoicedStoryLine,
    VoicedStoryScene,
)
from content_forge.core import (
    Asset,
    AssetRef,
    AudioTrack,
    MediaType,
    NormalizedPoint,
    OutputProfile,
    Project,
    ProjectState,
    Scene,
)
from content_forge.providers.ocr import OCRPixelRect
from content_forge.storage import LocalLibrary


def _asset(
    digest_character: str,
    *,
    media_type: MediaType,
    mime_type: str,
    width: int | None = None,
    height: int | None = None,
    duration_seconds: float | None = None,
    has_audio: bool | None = None,
) -> Asset:
    return Asset(
        sha256=digest_character * 64,
        media_type=media_type,
        mime_type=mime_type,
        size_bytes=128,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        has_audio=has_audio,
    )


def _fixture(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    focus_hint: SceneFocusHint | None = None,
):
    library = LocalLibrary(tmp_path / "runtime")
    image = library.database.put_asset(
        _asset(
            "1",
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            width=1200,
            height=1600,
            has_audio=False,
        )
    )
    voice_asset = library.database.put_asset(
        _asset(
            "2",
            media_type=MediaType.AUDIO,
            mime_type="audio/wav",
            duration_seconds=1.0,
            has_audio=True,
        )
    )
    music_asset = library.database.put_asset(
        _asset(
            "3",
            media_type=MediaType.AUDIO,
            mime_type="audio/wav",
            duration_seconds=10.0,
            has_audio=True,
        )
    )
    ambience_asset = library.database.put_asset(
        _asset(
            "4",
            media_type=MediaType.AUDIO,
            mime_type="audio/wav",
            duration_seconds=10.0,
            has_audio=True,
        )
    )

    voice = AudioTrack(
        track_type="voice",
        asset_ref=AssetRef(asset_id=voice_asset.asset_id, role="voice"),
        start_seconds=0.0,
        duration_seconds=1.0,
        properties={
            "pr22_owner": "pr22_voice_audio_v1",
            "line_id": "dlg_ocr_0000",
            "speaker_id": "alice",
            "cast_id": "protagonist",
            "cast_revision": 1,
            "audio_sha256": voice_asset.sha256,
        },
    )
    ambience = AudioTrack(
        track_type="ambience",
        asset_ref=AssetRef(asset_id=ambience_asset.asset_id, role="ambience"),
        start_seconds=0.0,
        duration_seconds=1.3,
        gain_db=-3.0,
    )
    scene = Scene(
        order=0,
        duration_seconds=1.3,
        media=AssetRef(asset_id=image.asset_id),
        audio_tracks=(voice, ambience),
    )
    music = AudioTrack(
        track_type="music",
        asset_ref=AssetRef(asset_id=music_asset.asset_id, role="music"),
        start_seconds=0.0,
        duration_seconds=1.3,
        gain_db=-8.0,
    )
    project = Project(
        content_kind="panel_sequence",
        state=ProjectState.READY,
        source_refs=(AssetRef(asset_id=image.asset_id),),
        scenes=(scene,),
        audio_tracks=(music,),
        output_profiles=(
            OutputProfile(
                profile_id="vertical",
                width=540,
                height=960,
                fps=30.0,
            ),
        ),
    )
    cue = TimedTextCue(
        phrase_index=0,
        text="Hello.",
        start_seconds=0.0,
        end_seconds=1.0,
    )
    voiced_line = VoicedStoryLine(
        line_id="dlg_ocr_0000",
        order=0,
        speaker_id="alice",
        source_text="Hello.",
        cast_id="protagonist",
        cast_revision=1,
        cast_definition_sha256="a" * 64,
        tts_cache_key="b" * 64,
        audio_asset_id=voice_asset.asset_id,
        audio_sha256=voice_asset.sha256,
        audio_duration_seconds=1.0,
        start_seconds=0.0,
        end_seconds=1.0,
        cues=(cue,),
    )
    voiced_scene = VoicedStoryScene(
        scene_id=scene.scene_id,
        scene_dialogue_digest="c" * 64,
        base_duration_seconds=2.0,
        duration_seconds=1.3,
        lines=(voiced_line,),
    )
    pr22 = ProjectVoicedStoryManifest(
        project_id=project.project_id,
        scenes=(voiced_scene,),
    )
    metadata = project.model_dump(mode="json")["metadata"]
    metadata["pr22_voiced_story"] = pr22.model_dump(mode="json")
    project = project.validated_copy(update={"metadata": metadata})
    library.save_project(project)

    dialogue_line = DialogueLine(
        line_id="dlg_ocr_0000",
        order=0,
        source_region_id="ocr_0000",
        text="Hello.",
        speaker_id="alice",
        source_bbox=OCRPixelRect(x_min=10, y_min=10, x_max=80, y_max=60),
    )
    dialogue = ProjectDialogueManifest(
        project_id=project.project_id,
        characters=(CharacterRecord(character_id="alice", display_name="Alice"),),
        scenes=(
            SceneDialogue(
                scene_id=scene.scene_id,
                extraction_digest="d" * 64,
                lines=(dialogue_line,),
                focus_hint=focus_hint,
            ),
        ),
    )
    monkeypatch.setattr(
        "content_forge.application.voiced_scene.validated_dialogue_manifest",
        lambda candidate: dialogue,
    )
    workflow = VoicedSceneWorkflow(library)
    monkeypatch.setattr(workflow, "_validated_pr22", lambda candidate: pr22)
    return library, workflow, project, pr22, voice, ambience, music


def test_pr23_materialization_preserves_pr22_voice_and_is_reversible(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hint = SceneFocusHint(mode="face", face=NormalizedPoint(x=0.62, y=0.42))
    library, workflow, project, pr22, voice, ambience, music = _fixture(
        tmp_path,
        monkeypatch,
        focus_hint=hint,
    )

    manifest = workflow.materialize(project.project_id)
    assert manifest.plan.pr22_manifest_sha256
    assert manifest.plan.scenes[0].camera_action == "focus_zoom"
    assert manifest.plan.scenes[0].camera_source == "face_hint"
    assert manifest.plan.passed

    stored = library.load_project(project.project_id)
    assert stored is not None
    scene = stored.scenes[0]
    current_voice = next(track for track in scene.audio_tracks if track.track_type == "voice")
    current_ambience = next(track for track in scene.audio_tracks if track.track_type == "ambience")
    assert current_voice == voice
    assert current_voice.properties.get("pr23_owner") is None
    assert current_ambience.properties.get("pr23_owner") == "pr23_voiced_mix_v1"
    assert current_ambience.properties.get("duck_db") == -6.0
    assert stored.audio_tracks[0].properties.get("pr23_owner") == "pr23_voiced_mix_v1"
    assert stored.audio_tracks[0].properties.get("duck_db") == -10.0
    assert scene.motion is not None
    assert scene.motion.motion_type == "focus_zoom"
    assert scene.motion.focus == hint.face
    assert scene.motion.properties.get("pr23_owner") == "pr23_camera_v1"
    assert stored.metadata["pr22_voiced_story"] == pr22.model_dump(mode="json")
    assert workflow.manifest(project.project_id) == manifest

    assert workflow.dematerialize(project.project_id)
    restored = library.load_project(project.project_id)
    assert restored is not None
    assert restored.scenes[0].motion is None
    assert restored.scenes[0].audio_tracks == (voice, ambience)
    assert restored.audio_tracks == (music,)
    assert restored.metadata["pr22_voiced_story"] == pr22.model_dump(mode="json")
    assert "pr23_voiced_scene" not in restored.metadata


def test_pr23_speaker_focus_does_not_invent_camera_geometry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, workflow, project, _, _, _, _ = _fixture(
        tmp_path,
        monkeypatch,
        focus_hint=SceneFocusHint(mode="speaker"),
    )

    plan = workflow.preview(project.project_id)
    scene = plan.scenes[0]
    assert scene.camera_action == "retain"
    assert scene.camera_source == "speaker_unresolved"
    assert scene.proposed_motion is None
    assert [issue.code for issue in scene.issues] == ["speaker_focus_geometry_missing"]
    assert plan.passed
    assert library.load_project(project.project_id) == project


def test_pr23_refuses_to_restore_owned_state_after_external_drift(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hint = SceneFocusHint(mode="face", face=NormalizedPoint(x=0.5, y=0.5))
    library, workflow, project, _, _, _, _ = _fixture(
        tmp_path,
        monkeypatch,
        focus_hint=hint,
    )
    workflow.materialize(project.project_id)
    stored = library.load_project(project.project_id)
    assert stored is not None
    scene = stored.scenes[0]
    assert scene.motion is not None
    drifted_motion = scene.motion.validated_copy(
        update={
            "properties": {
                **scene.motion.model_dump(mode="json")["properties"],
                "external_edit": True,
            }
        }
    )
    drifted_scene = scene.validated_copy(update={"motion": drifted_motion})
    drifted = stored.validated_copy(
        update={
            "scenes": (drifted_scene,),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    library.save_project(drifted)

    with pytest.raises(VoicedSceneConflictError, match="camera presentation state drifted"):
        workflow.dematerialize(project.project_id)
    assert library.load_project(project.project_id) == drifted
