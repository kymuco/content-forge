from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from content_forge.application import (
    ApplicationRepository,
    CharacterRecord,
    DialogueAssignment,
    DialogueWorkflow,
    LineTTSSettings,
    LineTTSWorkflow,
    PanelOCRWorkflow,
    TTSConflictError,
    TTSValidationError,
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
                    raw_text="Accepted dialogue line",
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
        self.on_synthesize = None
        self.bad_request_evidence = False

    def health(self) -> TTSProviderHealth:
        return TTSProviderHealth(
            provider_id="fake_tts",
            provider_version="1.2.3",
            model_id="synthetic-voice-model",
            config_sha256="d" * 64,
            available=True,
        )

    def synthesize(self, request: TTSRequest) -> TTSResult:
        self.calls.append(request)
        amplitude = 500 if request.voice_id == "voice-a" else 1000
        with wave.open(str(request.output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            frames = b"".join(
                int(amplitude if index % 2 == 0 else -amplitude).to_bytes(
                    2,
                    byteorder="little",
                    signed=True,
                )
                for index in range(480)
            )
            handle.writeframes(frames)
        if self.on_synthesize is not None:
            self.on_synthesize()
        payload = request.output_path.read_bytes()
        request_digest = semantic_tts_request_digest(request)
        if self.bad_request_evidence:
            request_digest = "f" * 64
        return TTSResult(
            audio_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            sample_rate_hz=24000,
            channels=1,
            sample_count=480,
            duration_seconds=0.02,
            evidence=TTSInvocationEvidence(
                provider_id="fake_tts",
                provider_version="1.2.3",
                model_id="synthetic-voice-model",
                engine="fake",
                request_sha256=request_digest,
                config_sha256="d" * 64,
                resolved_voice=request.voice_id,
                resolved_language=request.language,
            ),
        )


def _accepted_dialogue(tmp_path: Path):
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database)
    source = tmp_path / "panel.bin"
    source.write_bytes(b"pr20 panel")
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
    return library, project.project_id, scene.scene_id


def _reference_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 160)


def test_line_tts_synthesizes_verified_asset_and_reuses_exact_cache(tmp_path: Path) -> None:
    library, project_id, scene_id = _accepted_dialogue(tmp_path)
    provider = _TTSProvider()
    workflow = LineTTSWorkflow(library, provider)
    settings = LineTTSSettings(voice_id="voice-a", language="en")

    first = workflow.synthesize_line(project_id, scene_id, "dlg_ocr_0000", settings)
    assert len(provider.calls) == 1
    assert first.source_text == "Accepted dialogue line"
    assert first.speaker_id == "alice"
    assert first.duration_seconds == pytest.approx(0.02)
    asset = library.database.get_asset(first.asset_id)
    assert asset is not None
    assert asset.media_type is MediaType.AUDIO
    assert library.assets.verify(asset)

    repeated = workflow.synthesize_line(project_id, scene_id, "dlg_ocr_0000", settings)
    assert repeated == first
    assert len(provider.calls) == 1
    assert workflow.manifest(project_id).lines == (first,)


def test_line_tts_voice_or_style_change_invalidates_only_that_line(tmp_path: Path) -> None:
    library, project_id, scene_id = _accepted_dialogue(tmp_path)
    provider = _TTSProvider()
    workflow = LineTTSWorkflow(library, provider)
    first = workflow.synthesize_line(
        project_id,
        scene_id,
        "dlg_ocr_0000",
        LineTTSSettings(voice_id="voice-a", language="en"),
    )
    second = workflow.synthesize_line(
        project_id,
        scene_id,
        "dlg_ocr_0000",
        LineTTSSettings(
            voice_id="voice-b",
            language="en",
            instruction="More restrained",
        ),
    )
    assert len(provider.calls) == 2
    assert second.cache_key != first.cache_key
    assert second.audio_sha256 != first.audio_sha256
    assert workflow.manifest(project_id).lines == (second,)


def test_line_tts_reference_asset_is_verified_and_part_of_identity(tmp_path: Path) -> None:
    library, project_id, scene_id = _accepted_dialogue(tmp_path)
    source = tmp_path / "reference.wav"
    _reference_wav(source)
    reference = library.assets.ingest_file(
        source,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    provider = _TTSProvider()
    workflow = LineTTSWorkflow(library, provider)
    settings = LineTTSSettings(
        voice_id="clone-a",
        language="en",
        reference_asset_id=reference.asset_id,
        reference_text="Reference words",
    )
    record = workflow.synthesize_line(project_id, scene_id, "dlg_ocr_0000", settings)
    assert provider.calls[0].reference is not None
    assert provider.calls[0].reference.audio_sha256 == reference.sha256
    assert record.settings.reference_asset_id == reference.asset_id

    library.assets.resolve(reference).write_bytes(b"corrupt reference")
    with pytest.raises(TTSConflictError, match="reference asset"):
        workflow.manifest(project_id)


def test_line_tts_manifest_rejects_post_acceptance_speaker_tampering(tmp_path: Path) -> None:
    library, project_id, scene_id = _accepted_dialogue(tmp_path)
    provider = _TTSProvider()
    workflow = LineTTSWorkflow(library, provider)
    workflow.synthesize_line(
        project_id,
        scene_id,
        "dlg_ocr_0000",
        LineTTSSettings(voice_id="voice-a"),
    )
    project = library.load_project(project_id)
    assert project is not None
    metadata = project.model_dump(mode="json")["metadata"]
    metadata["pr20_tts"]["lines"][0]["speaker_id"] = "mallory"
    library.save_project(project.validated_copy(update={"metadata": metadata}))

    with pytest.raises(TTSConflictError, match="accepted dialogue line"):
        workflow.manifest(project_id)


def test_line_tts_manifest_rejects_generated_blob_corruption(tmp_path: Path) -> None:
    library, project_id, scene_id = _accepted_dialogue(tmp_path)
    provider = _TTSProvider()
    workflow = LineTTSWorkflow(library, provider)
    record = workflow.synthesize_line(
        project_id,
        scene_id,
        "dlg_ocr_0000",
        LineTTSSettings(voice_id="voice-a"),
    )
    asset = library.database.get_asset(record.asset_id)
    assert asset is not None
    library.assets.resolve(asset).write_bytes(b"corrupted")

    with pytest.raises(TTSConflictError, match="content verification"):
        workflow.manifest(project_id)


def test_line_tts_rejects_provider_evidence_mismatch_before_project_mutation(tmp_path: Path) -> None:
    library, project_id, scene_id = _accepted_dialogue(tmp_path)
    provider = _TTSProvider()
    provider.bad_request_evidence = True
    workflow = LineTTSWorkflow(library, provider)
    with pytest.raises(TTSValidationError, match="request evidence"):
        workflow.synthesize_line(
            project_id,
            scene_id,
            "dlg_ocr_0000",
            LineTTSSettings(voice_id="voice-a"),
        )
    project = library.load_project(project_id)
    assert project is not None
    assert tts_manifest(project).lines == ()


def test_line_tts_cas_rejects_project_change_during_expensive_synthesis(tmp_path: Path) -> None:
    library, project_id, scene_id = _accepted_dialogue(tmp_path)
    provider = _TTSProvider()
    workflow = LineTTSWorkflow(library, provider)

    def concurrent_change() -> None:
        project = library.load_project(project_id)
        assert project is not None
        metadata = project.model_dump(mode="json")["metadata"]
        metadata["concurrent_marker"] = "newer-state"
        library.save_project(project.validated_copy(update={"metadata": metadata}))

    provider.on_synthesize = concurrent_change
    with pytest.raises(TTSConflictError, match="changed concurrently"):
        workflow.synthesize_line(
            project_id,
            scene_id,
            "dlg_ocr_0000",
            LineTTSSettings(voice_id="voice-a"),
        )
    current = library.load_project(project_id)
    assert current is not None
    assert current.metadata["concurrent_marker"] == "newer-state"
    assert tts_manifest(current).lines == ()
