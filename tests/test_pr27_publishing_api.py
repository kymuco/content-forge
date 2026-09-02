from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers import (
    PublishInvocationEvidence,
    PublishResult,
    PublishingProviderHealth,
    semantic_publish_request_digest,
)

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
            "label": "pr27-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


class _ArtifactLoader:
    def __init__(self, artifact: RenderArtifactManifest) -> None:
        self.artifact = artifact

    def load_artifact(self, job_id: str, *, ffprobe_path: str, probe_timeout: float):
        assert job_id == self.artifact.job_id
        return self.artifact


def _artifact() -> RenderArtifactManifest:
    return RenderArtifactManifest(
        job_id=new_entity_id(EntityKind.JOB),
        project_id=new_entity_id(EntityKind.PROJECT),
        purpose="final",
        profile_id="youtube_shorts_1080p",
        render_plan_digest="1" * 64,
        command_manifest_digest="2" * 64,
        command_manifest_storage_key="commands/final.json",
        output_sha256="3" * 64,
        output_storage_key="renders/final.mp4",
        manifest_storage_key="renders/final.manifest.json",
        video_encoder="libx264",
        ffmpeg_version="fixture",
        bytes_written=4,
        elapsed_seconds=1.0,
        width=1080,
        height=1920,
        duration_seconds=8.0,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
    )


def _install_artifact(app, artifact: RenderArtifactManifest) -> None:
    path = app.state.library.paths.root / artifact.output_storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test")
    app.state.publishing.render_orchestrator = _ArtifactLoader(artifact)


def _candidate_payload(artifact: RenderArtifactManifest) -> dict[str, object]:
    return {
        "render_job_id": artifact.job_id,
        "target": {
            "provider_id": "fixture",
            "destination_id": "channel-main",
        },
        "metadata": {
            "title": "Approved upload",
            "description": "description",
            "tags": ["Genshin", "Raiden"],
            "visibility": "private",
        },
    }


class _SuccessfulProvider:
    def __init__(self) -> None:
        self.publish_calls = 0

    def health(self) -> PublishingProviderHealth:
        return PublishingProviderHealth(
            provider_id="fixture",
            provider_version="provider-v1",
            available=True,
            reason="token=SHOULD_NOT_PERSIST",
        )

    def publish(self, request, *, media_path: Path, idempotency_key: str) -> PublishResult:
        self.publish_calls += 1
        assert media_path.is_file()
        return PublishResult(
            disposition="published",
            remote_id="remote-1",
            remote_url="https://example.invalid/watch/remote-1",
            effective_at=datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc),
            evidence=PublishInvocationEvidence(
                provider_id="fixture",
                provider_version="provider-v1",
                request_sha256=semantic_publish_request_digest(request.request),
                idempotency_key=idempotency_key,
                output_sha256=request.request.artifact.output_sha256,
                destination_id=request.request.target.destination_id,
            ),
        )


def _candidate(client: TestClient, headers: dict[str, str], artifact: RenderArtifactManifest):
    response = client.post(
        "/api/v1/publishing/candidates",
        headers=headers,
        json=_candidate_payload(artifact),
    )
    assert response.status_code == 200
    return response.json()


def _approve(client: TestClient, headers: dict[str, str], candidate: dict[str, object]):
    response = client.post(
        "/api/v1/publishing/attempts",
        headers=headers,
        json={
            "request": candidate["request"],
            "confirm_request_sha256": candidate["request_sha256"],
            "note": "explicit human approval",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_pr27_api_provider_free_candidate_and_approval_preserve_prepared_state(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        artifact = _artifact()
        _install_artifact(app, artifact)
        headers = _paired_headers(client)

        candidate = _candidate(client, headers, artifact)
        assert candidate["provider_configured"] is False
        assert candidate["request"]["artifact"]["output_sha256"] == artifact.output_sha256

        approved = _approve(client, headers, candidate)
        attempt_id = approved["attempt"]["attempt_id"]
        assert approved["attempt"]["state"] == "prepared"
        assert approved["request_sha256"] == candidate["request_sha256"]
        assert approved["idempotency_key"] == candidate["idempotency_key"]

        execute = client.post(
            f"/api/v1/publishing/attempts/{attempt_id}/execute",
            headers=headers,
        )
        assert execute.status_code == 503
        assert execute.json()["detail"] == "publishing provider is not configured"

        stored = client.get(
            f"/api/v1/publishing/attempts/{attempt_id}",
            headers=headers,
        )
        assert stored.status_code == 200
        assert stored.json()["attempt"]["state"] == "prepared"
    finally:
        app.state.runtime_lease.close()


def test_pr27_api_execute_is_retry_safe_by_attempt_identity(tmp_path: Path) -> None:
    provider = _SuccessfulProvider()
    app = create_app(root=tmp_path, publishing_provider=provider)
    client = TestClient(app)
    try:
        artifact = _artifact()
        _install_artifact(app, artifact)
        headers = _paired_headers(client)
        candidate = _candidate(client, headers, artifact)
        approved = _approve(client, headers, candidate)
        attempt_id = approved["attempt"]["attempt_id"]

        first = client.post(
            f"/api/v1/publishing/attempts/{attempt_id}/execute",
            headers=headers,
        )
        assert first.status_code == 200
        assert first.json()["attempt"]["state"] == "succeeded"
        assert first.json()["attempt"]["provider_health"]["reason"] is None
        assert provider.publish_calls == 1

        replay = client.post(
            f"/api/v1/publishing/attempts/{attempt_id}/execute",
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert provider.publish_calls == 1
    finally:
        app.state.runtime_lease.close()


def test_pr27_api_requires_exact_digest_confirmation(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        artifact = _artifact()
        _install_artifact(app, artifact)
        headers = _paired_headers(client)
        candidate = _candidate(client, headers, artifact)

        rejected = client.post(
            "/api/v1/publishing/attempts",
            headers=headers,
            json={
                "request": candidate["request"],
                "confirm_request_sha256": "f" * 64,
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "publish approval digest does not match exact request"
    finally:
        app.state.runtime_lease.close()


def test_pr27_api_auth_precedes_json_parsing_and_body_is_bounded(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        unauthenticated = client.post(
            "/api/v1/publishing/candidates",
            headers=LOOPBACK_HEADERS,
            content=b"{not-json",
        )
        assert unauthenticated.status_code == 401

        headers = _paired_headers(client)
        wrong_media = client.post(
            "/api/v1/publishing/candidates",
            headers={**headers, "Content-Type": "application/json-patch+json"},
            content=b"{}",
        )
        assert wrong_media.status_code == 415

        oversized = client.post(
            "/api/v1/publishing/candidates",
            headers={
                **headers,
                "Content-Type": "application/json",
                "Content-Length": str(65 * 1024),
            },
            content=b"{}",
        )
        assert oversized.status_code == 413
    finally:
        app.state.runtime_lease.close()


def test_pr27_api_rejects_malformed_attempt_id_as_422(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        response = client.get(
            "/api/v1/publishing/attempts/not-a-publish-id",
            headers=headers,
        )
        assert response.status_code == 422
    finally:
        app.state.runtime_lease.close()


def test_pr27_api_remote_failure_does_not_expose_provider_secret(tmp_path: Path) -> None:
    class Provider(_SuccessfulProvider):
        def publish(self, request, *, media_path: Path, idempotency_key: str):
            raise RuntimeError("Authorization: Bearer SUPER_SECRET")

    app = create_app(root=tmp_path, publishing_provider=Provider())
    client = TestClient(app)
    try:
        artifact = _artifact()
        _install_artifact(app, artifact)
        headers = _paired_headers(client)
        candidate = _candidate(client, headers, artifact)
        approved = _approve(client, headers, candidate)
        attempt_id = approved["attempt"]["attempt_id"]

        failed = client.post(
            f"/api/v1/publishing/attempts/{attempt_id}/execute",
            headers=headers,
        )
        assert failed.status_code == 409
        assert "SUPER_SECRET" not in failed.text
        assert failed.json()["detail"] == "remote publish outcome is unknown; automatic retry is blocked"

        stored = client.get(
            f"/api/v1/publishing/attempts/{attempt_id}",
            headers=headers,
        )
        assert stored.status_code == 200
        assert stored.json()["attempt"]["state"] == "outcome_unknown"
        assert "SUPER_SECRET" not in stored.text
    finally:
        app.state.runtime_lease.close()
