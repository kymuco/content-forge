from __future__ import annotations

import wave

import pytest
from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import VoiceCastUnavailableError
from content_forge.core import MediaType, Project, Scene

LOOPBACK_HEADERS = {"Host": "localhost"}


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
            "label": "pr22-review-api",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def test_pr22_line_audio_serves_verified_current_wav(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    scene_id = Scene(order=0, duration_seconds=1.0).scene_id
    line_id = "dlg_ocr_0000"
    audio_path = tmp_path / "line.wav"
    with wave.open(str(audio_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x01\x00" * 240)
    asset = app.state.library.assets.ingest_file(
        audio_path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    monkeypatch.setattr(app.state.voiced_story, "line_audio", lambda *args: asset)
    try:
        headers = _paired_headers(client)
        response = client.get(
            f"/api/v1/voiced-story/projects/{project_id}/scenes/{scene_id}/lines/{line_id}/audio",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-forge-line"] == line_id
        assert response.headers["x-content-forge-audio-sha256"] == asset.sha256
        assert response.content == audio_path.read_bytes()
    finally:
        client.close()


def test_pr22_regenerate_without_provider_is_controlled_503(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(root=tmp_path / "runtime")
    client = TestClient(app)
    project_id = Project(content_kind="panel_sequence").project_id
    scene_id = Scene(order=0, duration_seconds=1.0).scene_id
    line_id = "dlg_ocr_0000"

    def unavailable(*args):
        raise VoiceCastUnavailableError(
            "voice cast synthesis requires a configured TTS provider"
        )

    monkeypatch.setattr(app.state.voiced_story, "regenerate_line", unavailable)
    try:
        headers = _paired_headers(client)
        response = client.post(
            f"/api/v1/voiced-story/projects/{project_id}/scenes/{scene_id}/lines/{line_id}/regenerate",
            headers=headers,
            json={},
        )
        assert response.status_code == 503
        assert "configured TTS provider" in response.json()["detail"]
    finally:
        client.close()
