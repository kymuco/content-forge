from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.core import EntityKind, new_entity_id
from content_forge.providers import (
    PublishArtifactRef,
    PublishDeclarations,
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    YouTubePublishingConfig,
    YouTubePublishingProvider,
    approve_publish_request,
)
from content_forge.web import static_path


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
            "label": "pr34-pytest",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


class _TargetOnlyProvider:
    def configured_target(self) -> PublishTarget:
        return PublishTarget(provider_id="fixture", destination_id="channel-main")


class _OpaqueProvider:
    pass


def _request(
    project_id: str,
    *,
    title: str,
    render_job_id: str | None = None,
    output_sha256: str | None = None,
) -> PublishRequest:
    return PublishRequest(
        contract_version="pr29_publish_contract_v2",
        artifact=PublishArtifactRef(
            project_id=project_id,
            render_job_id=render_job_id or new_entity_id(EntityKind.JOB),
            profile_id="youtube_shorts_1080p",
            render_plan_digest="1" * 64,
            output_sha256=output_sha256 or "2" * 64,
            bytes_written=123,
            width=1080,
            height=1920,
            duration_seconds=8.0,
            has_audio=True,
        ),
        target=PublishTarget(provider_id="fixture", destination_id="channel-main"),
        metadata=PublishMetadata(title=title, visibility="private"),
        declarations=PublishDeclarations(
            child_directed=False,
            contains_realistic_altered_or_synthetic_media=False,
        ),
    )


def _prepare(
    app,
    project_id: str,
    *,
    title: str,
    render_job_id: str | None = None,
    output_sha256: str | None = None,
) -> tuple[str, PublishRequest]:
    request = _request(
        project_id,
        title=title,
        render_job_id=render_job_id,
        output_sha256=output_sha256,
    )
    approved = approve_publish_request(request)
    attempt = app.state.library.publishing.prepare_attempt(approved)
    return attempt.attempt_id, request


def _projection_url(project_id: str, request: PublishRequest, *, limit: int = 20) -> str:
    artifact = request.artifact
    return (
        f"/api/v1/publishing/projects/{project_id}"
        f"?render_job_id={artifact.render_job_id}"
        f"&output_sha256={artifact.output_sha256}"
        f"&limit={limit}"
    )


def test_pr34_project_projection_is_authenticated_and_filters_exact_final_before_limit(
    tmp_path: Path,
) -> None:
    app = create_app(root=tmp_path, publishing_provider=_TargetOnlyProvider())
    client = TestClient(app)
    try:
        project_id = new_entity_id(EntityKind.PROJECT)
        unrelated_id = new_entity_id(EntityKind.PROJECT)
        exact_job_id = new_entity_id(EntityKind.JOB)
        exact_sha = "a" * 64
        expected_attempt, exact_request = _prepare(
            app,
            project_id,
            title="Project upload",
            render_job_id=exact_job_id,
            output_sha256=exact_sha,
        )
        # Newer attempts for both the same Project/different final and another Project
        # must not consume the exact-final projection limit.
        _prepare(app, project_id, title="Stale project final", output_sha256="b" * 64)
        _prepare(app, unrelated_id, title="Unrelated newer upload", output_sha256="c" * 64)

        url = _projection_url(project_id, exact_request, limit=1)
        unauthenticated = client.get(url)
        assert unauthenticated.status_code == 401

        headers = _paired_headers(client)
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["project_id"] == project_id
        assert payload["render_job_id"] == exact_job_id
        assert payload["output_sha256"] == exact_sha
        assert payload["provider_configured"] is True
        assert payload["configured_target"] == {
            "provider_id": "fixture",
            "destination_id": "channel-main",
        }
        assert payload["preferred_contract_version"] == "pr29_publish_contract_v2"
        assert [item["attempt"]["attempt_id"] for item in payload["items"]] == [expected_attempt]
        assert all(
            item["request"]["artifact"]["project_id"] == project_id
            and item["request"]["artifact"]["render_job_id"] == exact_job_id
            and item["request"]["artifact"]["output_sha256"] == exact_sha
            for item in payload["items"]
        )
    finally:
        app.state.runtime_lease.close()


def test_pr34_project_projection_requires_valid_exact_final_identity(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        project_id = new_entity_id(EntityKind.PROJECT)
        job_id = new_entity_id(EntityKind.JOB)

        malformed_project = client.get(
            f"/api/v1/publishing/projects/not-a-project-id"
            f"?render_job_id={job_id}&output_sha256={'a' * 64}",
            headers=headers,
        )
        assert malformed_project.status_code == 422

        malformed_job = client.get(
            f"/api/v1/publishing/projects/{project_id}"
            f"?render_job_id=not-a-job&output_sha256={'a' * 64}",
            headers=headers,
        )
        assert malformed_job.status_code == 422

        malformed_sha = client.get(
            f"/api/v1/publishing/projects/{project_id}"
            f"?render_job_id={job_id}&output_sha256=not-a-sha",
            headers=headers,
        )
        assert malformed_sha.status_code == 422
    finally:
        app.state.runtime_lease.close()


def test_pr34_unknown_provider_never_invents_a_phone_destination(tmp_path: Path) -> None:
    app = create_app(root=tmp_path, publishing_provider=_OpaqueProvider())
    client = TestClient(app)
    try:
        headers = _paired_headers(client)
        project_id = new_entity_id(EntityKind.PROJECT)
        request = _request(project_id, title="No target")
        response = client.get(_projection_url(project_id, request), headers=headers)
        assert response.status_code == 200
        assert response.json()["provider_configured"] is True
        assert response.json()["configured_target"] is None
    finally:
        app.state.runtime_lease.close()


def test_pr34_youtube_target_projection_contains_channel_identity_only(tmp_path: Path) -> None:
    token_path = (tmp_path / "youtube-token.json").resolve()
    provider = YouTubePublishingProvider(
        YouTubePublishingConfig(
            token_path=str(token_path),
            channel_id="UC_PR34_CHANNEL",
        )
    )

    target = provider.configured_target()
    assert target.model_dump(mode="json") == {
        "provider_id": "youtube",
        "destination_id": "UC_PR34_CHANNEL",
    }
    serialized = str(target.model_dump(mode="json"))
    assert str(token_path) not in serialized
    assert "token_path" not in serialized


def test_pr34_served_project_flow_emits_non_authoritative_publish_hooks(tmp_path: Path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        response = client.get("/app/production-home.js")
        assert response.status_code == 200
        script = response.text
        assert "content-forge:project-flow-rendered" in script
        assert "detail: { project }" in script
        assert "content-forge:project-flow-closed" in script
        assert script.count("content-forge:project-flow-rendered") == 1
        assert script.count("content-forge:project-flow-closed") == 1
    finally:
        app.state.runtime_lease.close()


def test_pr34_phone_publish_source_uses_existing_publish_authority() -> None:
    script = static_path("project-publishing.js").read_text(encoding="utf-8")

    assert "content-forge:project-flow-rendered" in script
    assert 'await apiJson("publishing/status")' in script
    assert "render_job_id: project.final.job_id" in script
    assert 'contract_version: "pr29_publish_contract_v2"' in script
    assert "child_directed: requiredDeclaration" in script
    assert "contains_realistic_altered_or_synthetic_media: requiredDeclaration" in script
    assert 'apiJson("publishing/candidates"' in script
    assert 'apiJson("publishing/attempts"' in script
    assert "/execute`" in script
    assert "artifact.project_id !== project.project_id" in script
    assert "artifact.render_job_id !== final.job_id" in script
    assert "artifact.output_sha256 !== final.output_sha256" in script
    assert "Routine replacement publishing is blocked to prevent a duplicate upload." in script
    assert "Multiple active publish attempts exist for this exact final." in script
    assert "configured_target" in script
    assert "innerHTML" not in script

    routine_form = script.split("function renderForm(", 1)[1].split("async function renderStage", 1)[0]
    assert 'document.getElementById("publishing-render-job")' not in routine_form
    assert 'document.getElementById("publishing-provider-id")' not in routine_form
    assert 'document.getElementById("publishing-destination-id")' not in routine_form

    approval = script.split('action("Approve exact publish request"', 1)[1].split(
        'action("Edit publish details"', 1
    )[0]
    assert 'publishing/attempts"' in approval
    assert "/execute" not in approval


def test_pr34_served_phone_bundle_queries_exact_final_and_prioritizes_remote_risk(
    tmp_path: Path,
) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        response = client.get("/app/publishing.js")
        assert response.status_code == 200
        script = response.text
        assert "publishing-candidate-form" in script
        assert "project-publishing-stage" in script
        assert "content-forge:project-flow-rendered" in script
        assert "?render_job_id=${encodeURIComponent(project.final.job_id)}" in script
        assert "&output_sha256=${encodeURIComponent(project.final.output_sha256)}" in script
        assert "context.render_job_id !== project.final.job_id" in script
        assert "context.output_sha256 !== project.final.output_sha256" in script
        assert 'if (state === "outcome_unknown") return 5;' in script
        assert 'if (state === "running") return 4;' in script
        assert 'if (state === "succeeded") return 3;' in script
    finally:
        app.state.runtime_lease.close()


def test_pr34_installed_pwa_advances_from_pr33_shell() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'const PR33_CACHE_NAME = `${CACHE_PREFIX}v19`' in worker
    assert 'const CACHE_NAME = `${CACHE_PREFIX}v20`' in worker
    assert "key === PR33_CACHE_NAME" in worker
    assert 'appUrl("publishing.js")' in worker


def test_pr34_project_publishing_javascript_has_valid_syntax() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    completed = subprocess.run(
        [node, "--check", str(static_path("project-publishing.js"))],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
