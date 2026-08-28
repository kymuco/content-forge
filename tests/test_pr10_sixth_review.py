from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.application import _review_base as _base
from content_forge.application.review import ReviewConflictError, ReviewService
from content_forge.core import (
    Asset,
    AssetRef,
    AttentionMode,
    MediaType,
    Project,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
    ReviewTask,
    Variant,
)
from content_forge.storage import LocalLibrary


def _task(project: Project, task_type: str) -> ReviewTask:
    return next(task for task in project.review_tasks if task.task_type == task_type)


def _image_asset(library: LocalLibrary, seed: int) -> Asset:
    return library.database.put_asset(
        Asset(
            sha256=f"{seed:064x}",
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=100 + seed,
            width=1080,
            height=1920,
        )
    )


def test_forged_manual_reentry_checkpoint_without_sqlite_receipt_is_rejected(
    tmp_path,
) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)
    project = library.save_project(Project(content_kind="image", state=ProjectState.INBOX))
    prepared = service.bootstrap_project(project.project_id)
    assert prepared.metadata.get("review_renderable") is False

    metadata = dict(prepared.metadata)
    metadata.pop("pr10_review_initialized", None)
    metadata["pr10_manual_reentry_pending"] = True
    metadata["pr10_manual_reentry_receipt_job_id"] = "job_" + "1" * 32
    forged = library.save_project(
        prepared.validated_copy(
            update={"metadata": metadata, "updated_at": datetime.now(timezone.utc)}
        )
    )

    with pytest.raises(
        ReviewConflictError,
        match="manual re-entry durable receipt is missing",
    ):
        service.bootstrap_project(forged.project_id)

    current = service.get_project(forged.project_id)
    assert current.metadata.get("pr10_manual_reentry_pending") is True
    assert not current.metadata.get("pr10_review_initialized")
    assert _task(current, "source_setup").status is ReviewStatus.OPEN


def test_restart_recovery_skips_malformed_project_manifest_and_recovers_valid(
    tmp_path,
) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)

    asset = _image_asset(library, 1701)
    project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )
    prepared = service.bootstrap_project(project.project_id)
    preview = _task(prepared, "preview_approval")
    claimed = preview.validated_copy(
        update={
            "payload": {
                "status": "rendering",
                "claim_id": "valid-recovery-claim",
                "render_plan_digest": "a" * 64,
                "project_revision_digest": "b" * 64,
            }
        }
    )
    prepared = library.save_project(
        prepared.validated_copy(
            update={
                "review_tasks": tuple(
                    claimed if task.review_task_id == preview.review_task_id else task
                    for task in prepared.review_tasks
                ),
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )

    malformed = library.save_project(
        Project(content_kind="image", state=ProjectState.INBOX)
    )
    with library.database.transaction() as connection:
        connection.execute(
            "UPDATE projects SET manifest_json = ? WHERE project_id = ?",
            ("{", malformed.project_id),
        )

    service.reconcile_persisted_state()

    recovered = service.get_project(prepared.project_id)
    assert _task(recovered, "preview_approval").payload == {"status": "not_rendered"}
    with library.database.connection() as connection:
        raw = connection.execute(
            "SELECT manifest_json FROM projects WHERE project_id = ?",
            (malformed.project_id,),
        ).fetchone()
    assert raw is not None
    assert raw["manifest_json"] == "{"


def test_restart_recovery_quarantines_malformed_render_job_row(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)

    asset = _image_asset(library, 1702)
    project = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )
    prepared = service.bootstrap_project(project.project_id)
    metadata = dict(prepared.metadata)
    metadata["active_final_plan_digest"] = "c" * 64
    rendering = library.save_project(
        prepared.validated_copy(
            update={
                "state": ProjectState.RENDERING,
                "metadata": metadata,
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )

    now = datetime.now(timezone.utc).isoformat()
    malformed_job_id = "job_" + "2" * 32
    with library.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, project_id, job_type, state,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, 'render', 'succeeded', ?, ?, ?)
            """,
            (malformed_job_id, rendering.project_id, "{", now, now),
        )

    service.reconcile_persisted_state()

    recovered = service.get_project(rendering.project_id)
    assert recovered.state is ProjectState.READY
    assert "active_final_plan_digest" not in recovered.metadata
    assert recovered.metadata.get("last_final_render_error") == (
        "final render recovered after process restart"
    )
    with library.database.connection() as connection:
        raw = connection.execute(
            "SELECT payload_json FROM jobs WHERE job_id = ?",
            (malformed_job_id,),
        ).fetchone()
    assert raw is not None
    assert raw["payload_json"] == "{"


def test_queue_filters_authority_before_limit(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    service = ReviewService(library)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)

    rows = []
    for _ in range(500):
        project = Project(content_kind="image", state=ProjectState.INBOX)
        variant = Variant()
        hook = ReviewTask(
            project_id=project.project_id,
            task_type="hook",
            attention=AttentionMode.REVIEW,
            priority=ReviewPriority.BLOCKING,
            blocking=True,
            payload={"variant_id": variant.variant_id, "current": None},
            created_at=old,
        )
        project = project.validated_copy(
            update={
                "variants": (variant,),
                "review_tasks": (hook,),
                "metadata": {"review_variant_id": variant.variant_id},
            }
        )
        rows.append(
            (
                project.project_id,
                project.content_kind,
                project.state.value,
                _base.dump_json(project),
                project.created_at.isoformat(),
                project.updated_at.isoformat(),
            )
        )

    with library.database.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO projects(
                project_id, content_kind, state, manifest_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    asset = _image_asset(library, 1703)
    valid = library.save_project(
        Project(
            content_kind="image",
            state=ProjectState.INBOX,
            source_refs=(AssetRef(asset_id=asset.asset_id),),
        )
    )
    valid = service.bootstrap_project(valid.project_id)
    valid_hook = _task(valid, "hook")

    queue = service.list_queue(limit=1)

    assert len(queue["items"]) == 1
    assert queue["items"][0]["project_id"] == valid.project_id
    assert queue["items"][0]["task"]["review_task_id"] == valid_hook.review_task_id
