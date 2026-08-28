from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import ReviewService as PackageReviewService
from content_forge.application.review import ReviewService
from content_forge.core import (
    Asset,
    AssetRef,
    MediaType,
    Project,
    ProjectState,
)
from content_forge.storage import LocalLibrary
from content_forge.web import static_path


LOOPBACK_HEADERS = {"Host": "localhost"}


class FinalArtifactLoader:
    def __init__(self, artifact) -> None:
        self.artifact = artifact

    def load_artifact(self, job_id, *, ffprobe_path="ffprobe", probe_timeout=20.0):
        if job_id != self.artifact.job_id:
            return None
        return self.artifact


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
            "label": "pr10-seventh-review",
        },
    )
    assert exchanged.status_code == 200
    return {"Authorization": f"Bearer {exchanged.json()['token']}"}


def _visual_project(library: LocalLibrary) -> Project:
    asset = library.database.put_asset(
        Asset(
            sha256="7" * 64,
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=101,
            width=1080,
            height=1920,
        )
    )
    return library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )


def test_public_review_imports_share_seventh_pass_hardened_class() -> None:
    assert PackageReviewService is ReviewService
    assert ReviewService.__module__.endswith("review_seventh_hardening")


def test_qc_recovery_rejects_incomplete_final_identity(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    bootstrap = ReviewService(library)
    project = bootstrap.bootstrap_project(_visual_project(library).project_id)

    job_id = "job_01seventhpassfinalreceipt000000000000"
    digest = "d" * 64
    output_sha = "e" * 64
    artifact = SimpleNamespace(
        job_id=job_id,
        project_id=project.project_id,
        purpose="final",
        render_plan_digest=digest,
        output_sha256=output_sha,
    )
    service = ReviewService(library, orchestrator=FinalArtifactLoader(artifact))

    metadata = dict(project.metadata)
    metadata["final_render_job_id"] = job_id
    metadata["final_render_plan_digest"] = digest
    metadata.pop("final_output_sha256", None)
    library.save_project(
        project.validated_copy(
            update={"state": ProjectState.QC, "metadata": metadata}
        )
    )

    service.reconcile_persisted_state()

    recovered = service.get_project(project.project_id)
    assert recovered.state is ProjectState.READY
    assert recovered.metadata["last_final_render_error"] == (
        "final QC recovery found no valid artifact"
    )


def test_bulk_prepare_is_authenticated_and_not_limited_by_intake_page(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        project_ids = []
        for _ in range(125):
            project = app.state.library.save_project(
                Project(content_kind="note", state=ProjectState.INBOX)
            )
            project_ids.append(project.project_id)

        unauthorized = client.post("/api/v1/review-prepare")
        assert unauthorized.status_code == 401

        headers = _paired_headers(client)
        prepared = client.post("/api/v1/review-prepare", headers=headers)
        assert prepared.status_code == 200
        payload = prepared.json()
        assert payload["eligible"] == 125
        assert payload["processed"] == 125
        assert payload["changed"] == 125
        assert payload["failed"] == 0

        for project_id in project_ids:
            project = app.state.review.get_project(project_id)
            assert project.metadata["pr10_review_initialized"] is True
    finally:
        app.state.runtime_lease.close()


def test_pwa_prepare_uses_server_side_complete_project_enumeration() -> None:
    client = static_path("review.js").read_text(encoding="utf-8")
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'apiJson("review-prepare", { method: "POST" })' in client
    assert "inbox?limit=100" not in client
    assert "${CACHE_PREFIX}v8" in worker
    assert 'appUrl("review.js")' in worker
