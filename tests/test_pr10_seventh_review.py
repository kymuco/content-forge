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
from content_forge.timeline import render_plan_digest
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


def _visual_project(library: LocalLibrary, *, sha_char: str = "7") -> Project:
    asset = library.database.put_asset(
        Asset(
            sha256=sha_char * 64,
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


def _task(project: Project, task_type: str):
    return next(task for task in project.review_tasks if task.task_type == task_type)


def _resolve_final_inputs(service: ReviewService, project: Project) -> Project:
    hook = _task(project, "hook")
    project = service.resolve_task(
        project.project_id,
        hook.review_task_id,
        "receipt-bound hook",
    )
    crop = _task(project, "crop_confirmation")
    return service.resolve_task(
        project.project_id,
        crop.review_task_id,
        {"crops": {scene.scene_id: None for scene in project.scenes}},
    )


def _manual_reentry_count(library: LocalLibrary) -> int:
    with library.database.connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE job_type = 'review_manual_reentry'"
        ).fetchone()
    assert row is not None
    return int(row["count"])


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


def test_qc_recovery_rejects_complete_receipt_for_stale_final_plan(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    bootstrap = ReviewService(library)
    project = bootstrap.bootstrap_project(_visual_project(library).project_id)
    project = _resolve_final_inputs(bootstrap, project)
    old_plan = bootstrap._compile_plan(project, "shorts_final")
    old_digest = render_plan_digest(old_plan)

    job_id = "job_01eighthpassstalefinal00000000000000"
    output_sha = "f" * 64
    artifact = SimpleNamespace(
        job_id=job_id,
        project_id=project.project_id,
        purpose="final",
        render_plan_digest=old_digest,
        output_sha256=output_sha,
    )

    variant = project.variants[0].validated_copy(
        update={"hook": "canonical hook changed after old final"}
    )
    metadata = dict(project.metadata)
    metadata.update(
        {
            "final_render_job_id": job_id,
            "final_render_plan_digest": old_digest,
            "final_output_sha256": output_sha,
        }
    )
    stale = project.validated_copy(
        update={
            "state": ProjectState.QC,
            "variants": (variant, *project.variants[1:]),
            "metadata": metadata,
        }
    )
    library.save_project(stale)

    service = ReviewService(library, orchestrator=FinalArtifactLoader(artifact))
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

        # The initial non-renderable evaluation records a setup-input fingerprint without
        # creating MANUAL re-entry receipts. Repeating the same bulk action must be a true
        # no-op rather than growing the durable jobs table forever.
        assert _manual_reentry_count(app.state.library) == 0
        repeated = client.post("/api/v1/review-prepare", headers=headers)
        assert repeated.status_code == 200
        repeat_payload = repeated.json()
        assert repeat_payload["eligible"] == 0
        assert repeat_payload["processed"] == 0
        assert repeat_payload["changed"] == 0
        assert repeat_payload["failed"] == 0
        assert _manual_reentry_count(app.state.library) == 0
    finally:
        app.state.runtime_lease.close()


def test_bulk_prepare_rechecks_once_after_setup_inputs_change(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        project = app.state.library.save_project(
            Project(content_kind="note", state=ProjectState.INBOX)
        )
        headers = _paired_headers(client)

        first = client.post("/api/v1/review-prepare", headers=headers)
        assert first.status_code == 200
        assert first.json()["eligible"] == 1
        assert _manual_reentry_count(app.state.library) == 0

        unchanged = client.post("/api/v1/review-prepare", headers=headers)
        assert unchanged.status_code == 200
        assert unchanged.json()["eligible"] == 0
        assert _manual_reentry_count(app.state.library) == 0

        asset = app.state.library.database.put_asset(
            Asset(
                sha256="8" * 64,
                media_type=MediaType.IMAGE,
                mime_type="image/png",
                size_bytes=202,
                width=1080,
                height=1920,
            )
        )
        current = app.state.review.get_project(project.project_id)
        app.state.library.save_project(
            current.validated_copy(
                update={"source_refs": (AssetRef(asset_id=asset.asset_id),)}
            )
        )

        repaired = client.post("/api/v1/review-prepare", headers=headers)
        assert repaired.status_code == 200
        repair_payload = repaired.json()
        assert repair_payload["eligible"] == 1
        assert repair_payload["processed"] == 1
        assert repair_payload["changed"] == 1
        assert repair_payload["failed"] == 0
        promoted = app.state.review.get_project(project.project_id)
        assert promoted.metadata["review_renderable"] is True
        assert _manual_reentry_count(app.state.library) == 1

        stable = client.post("/api/v1/review-prepare", headers=headers)
        assert stable.status_code == 200
        assert stable.json()["eligible"] == 0
        assert _manual_reentry_count(app.state.library) == 1
    finally:
        app.state.runtime_lease.close()


def test_pwa_prepare_uses_server_side_complete_project_enumeration() -> None:
    client = static_path("review.js").read_text(encoding="utf-8")
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'apiJson("review-prepare", { method: "POST" })' in client
    assert "inbox?limit=100" not in client
    assert "${CACHE_PREFIX}v8" in worker
    assert 'appUrl("review.js")' in worker
