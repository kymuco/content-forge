from __future__ import annotations

import hashlib
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import (
    ApplicationRepository,
    CharacterRecord,
    DialogueAssignment,
    PanelOCRWorkflow,
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

LOOPBACK_HEADERS = {"Host": "localhost"}


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
                    raw_text="Preview this accepted line",
                    confidence=0.99,
                    polygon=(
                        OCRPoint(x=5, y=5),
                        OCRPoint(x=85, y=5),
                        OCRPoint(x=85, y=25),
                        OCRPoint(x=5, y=25),
                    ),
                    bbox=OCRPixelRect(x_min=5, y_min=5, x_max=85, y_max=25),
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
        with wave.open(str(request.output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(b"\x10\x00\xf0\xff" * 240)
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


def _paired_headers(client: TestClient) -> dict[str, str]:
    challenge = client.post("/api/v1/pairing/challenges", headers=LOOPBACK_HEADERS)
    assert challenge.status_code == 201
    payload = challenge.json()
    exchanged = client.post(
        "/api/v1/pairing/exchange",
        headers=LOOPBACK_HEADERS,
        json={
            "challenge_id": payload["challenge_id"],
            "code": payload["code"],
            "label": "pr21-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def _accepted_dialogue(app, tmp_path: Path) -> tuple[str, str]:
    library = app.state.library
    source = tmp_path / "voice-cast-api-panel.bin"
    source.write_bytes(b"pr21 voice cast api panel")
    ingested = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
    )
    asset = ingested.asset.validated_copy(update={"width": 100, "height": 100})
    ApplicationRepository(library.database).enrich_asset(asset)
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
    app.state.dialogue.register_character(
        project.project_id,
        CharacterRecord(character_id="alice", display_name="Alice"),
    )
    prepared = app.state.dialogue.prepare_scene_assignment(project.project_id, scene.scene_id)
    task = next(item for item in prepared.review_tasks if item.task_type == "dialogue_scene_assignment")
    app.state.dialogue.apply_scene_assignment(
        project.project_id,
        task.review_task_id,
        DialogueAssignment(
            reading_order=("ocr_0000",),
            speaker_by_region={"ocr_0000": "alice"},
        ),
    )
    return project.project_id, scene.scene_id


def _cast_payload(voice_id: str = "voice-a") -> dict[str, object]:
    return {
        "cast_id": "protagonist",
        "display_name": "Protagonist",
        "settings": {
            "voice_id": voice_id,
            "language": "en",
        },
    }


def test_voice_cast_http_registry_binding_and_preview(tmp_path: Path) -> None:
    provider = _TTSProvider()
    app = create_app(root=tmp_path, tts_provider=provider)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        project_id, _scene_id = _accepted_dialogue(app, tmp_path)

        created = client.post("/api/v1/voice-cast", headers=headers, json=_cast_payload())
        assert created.status_code == 201
        revision = created.json()
        assert revision["cast_id"] == "protagonist"
        assert revision["revision"] == 1
        assert revision["settings"]["voice_id"] == "voice-a"

        idempotent = client.post("/api/v1/voice-cast", headers=headers, json=_cast_payload())
        assert idempotent.status_code == 200
        assert idempotent.json() == revision

        listed = client.get("/api/v1/voice-cast", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["items"] == [revision]

        project_view = client.get(
            f"/api/v1/voice-cast/projects/{project_id}", headers=headers
        )
        assert project_view.status_code == 200
        assert project_view.json()["characters"][0]["character_id"] == "alice"
        assert project_view.json()["bindings"] == []

        bound = client.put(
            f"/api/v1/voice-cast/projects/{project_id}/characters/alice",
            headers=headers,
            json={"cast_id": "protagonist", "cast_revision": 1},
        )
        assert bound.status_code == 200
        binding = bound.json()["bindings"][0]
        assert binding["character_id"] == "alice"
        assert binding["cast_id"] == "protagonist"
        assert binding["cast_revision"] == 1

        preview = client.post(
            f"/api/v1/voice-cast/projects/{project_id}/characters/alice/preview",
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("audio/wav")
        assert preview.headers["x-content-forge-cast"] == "protagonist@1"
        assert len(preview.headers["x-content-forge-audio-sha256"]) == 64
        assert preview.content.startswith(b"RIFF")
        assert len(provider.calls) == 1
        assert provider.calls[0].text == "Preview this accepted line"
        assert provider.calls[0].voice_id == "voice-a"

        # Preview uses the normal PR20 line cache rather than an ephemeral audio path.
        repeated = client.post(
            f"/api/v1/voice-cast/projects/{project_id}/characters/alice/preview",
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        assert repeated.status_code == 200
        assert repeated.content == preview.content
        assert len(provider.calls) == 1
    finally:
        app.state.runtime_lease.close()


def test_voice_cast_preview_is_503_without_optional_tts_provider(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        project_id, _scene_id = _accepted_dialogue(app, tmp_path)
        assert client.post(
            "/api/v1/voice-cast", headers=headers, json=_cast_payload()
        ).status_code == 201
        assert client.put(
            f"/api/v1/voice-cast/projects/{project_id}/characters/alice",
            headers=headers,
            json={"cast_id": "protagonist"},
        ).status_code == 200

        preview = client.post(
            f"/api/v1/voice-cast/projects/{project_id}/characters/alice/preview",
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        assert preview.status_code == 503
        assert "configured TTS provider" in preview.json()["detail"]
    finally:
        app.state.runtime_lease.close()


def test_voice_cast_transport_authenticates_and_bounds_before_json_parsing(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        unauthenticated = client.post(
            "/api/v1/voice-cast",
            headers={"Content-Type": "application/json"},
            content=b"{not-json",
        )
        assert unauthenticated.status_code == 401

        headers = _paired_headers(client)
        unsupported = client.post(
            "/api/v1/voice-cast",
            headers={**headers, "Content-Type": "text/plain"},
            content=b"not-json",
        )
        assert unsupported.status_code == 415

        oversized = client.post(
            "/api/v1/voice-cast",
            headers={
                **headers,
                "Content-Type": "application/json",
                "Content-Length": str(129 * 1024),
            },
            content=b"{}",
        )
        assert oversized.status_code == 413
    finally:
        app.state.runtime_lease.close()
