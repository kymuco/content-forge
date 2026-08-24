from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_forge.core import AssetRef, MediaType, Project, Scene, TemplateRef, Variant
from content_forge.orchestration import (
    RenderJobIntegrityError,
    RenderJobStateError,
    RenderOrchestrator,
)
from content_forge.profiles import (
    SHORTS_FINAL_PROFILE_ID,
    SHORTS_PREVIEW_PROFILE_ID,
    shorts_final_profile,
    shorts_preview_profile,
)
from content_forge.render.ffmpeg import (
    CancellationToken,
    FFmpegBackendError,
    FFmpegCapabilities,
)
from content_forge.storage import LocalLibrary
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    compile_hook_overlay,
)


def _fixture(tmp_path: Path) -> tuple[LocalLibrary, Project, str]:
    library = LocalLibrary(tmp_path / "runtime")
    source = tmp_path / "source.ppm"
    source.write_text(
        "P3\n4 6\n255\n" + "\n".join(["20 80 200"] * 24) + "\n",
        encoding="ascii",
    )
    ingest = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
    )
    project = Project(
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        variants=(Variant(language="en", hook="Persistent preview hook"),),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.4,
                media=AssetRef(asset_id=ingest.asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(), shorts_final_profile()),
    )
    library.save_project(project)
    return library, project, ingest.asset.asset_id


def _capabilities() -> FFmpegCapabilities:
    return FFmpegCapabilities(
        ffmpeg_path="/definitely/missing/content-forge-ffmpeg",
        ffprobe_path="/definitely/missing/content-forge-ffprobe",
        ffmpeg_version="ffmpeg synthetic-test",
        ffprobe_version="ffprobe synthetic-test",
        encoders=("libx264",),
        filters=(
            "aformat",
            "amix",
            "anullsrc",
            "asetpts",
            "atrim",
            "color",
            "concat",
            "crop",
            "drawtext",
            "format",
            "fps",
            "overlay",
            "scale",
            "setpts",
            "trim",
            "volume",
        ),
    )


def test_submit_persists_immutable_plan_snapshot_and_job_metadata(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    orchestrator = RenderOrchestrator(library)

    job = orchestrator.submit(plan, purpose="preview")

    stored = library.database.get_job(job.job_id)
    assert stored == job
    assert stored is not None
    assert stored.state == "queued"
    assert stored.job_type == "render"
    assert stored.payload["purpose"] == "preview"
    assert stored.payload["profile_id"] == SHORTS_PREVIEW_PROFILE_ID
    assert stored.payload["plan_storage_key"].startswith(
        f"renders/{project.project_id}/{job.job_id}/"
    )
    assert stored.payload["command_manifest_storage_key"].endswith(
        "/command-manifest.json"
    )
    assert str(library.paths.root) not in stored.payload["plan_storage_key"]
    assert str(library.paths.root) not in stored.payload["command_manifest_storage_key"]
    assert orchestrator.load_plan(job.job_id) == plan


def test_preview_and_final_jobs_keep_distinct_profile_identity(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    orchestrator = RenderOrchestrator(library)
    preview = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    final = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_FINAL_PROFILE_ID,
    )

    preview_job = orchestrator.submit(preview, purpose="preview")
    final_job = orchestrator.submit(final, purpose="final")

    assert preview_job.payload["profile_id"] == SHORTS_PREVIEW_PROFILE_ID
    assert final_job.payload["profile_id"] == SHORTS_FINAL_PROFILE_ID
    assert preview_job.payload["output_storage_key"] != final_job.payload[
        "output_storage_key"
    ]
    assert orchestrator.load_plan(preview_job.job_id).output_profile.width == 540
    assert orchestrator.load_plan(final_job.job_id).output_profile.width == 1080


def test_submit_rejects_mislabeled_render_purpose(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )

    with pytest.raises(RenderJobIntegrityError, match="render purpose"):
        RenderOrchestrator(library).submit(plan, purpose="final")


def test_submit_rejects_asset_metadata_that_no_longer_matches_library(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    changed_asset = plan.assets[0].validated_copy(update={"sha256": "f" * 64})
    changed_plan = plan.validated_copy(update={"assets": (changed_asset,)})

    with pytest.raises(RenderJobIntegrityError, match="asset metadata differs"):
        RenderOrchestrator(library).submit(changed_plan, purpose="preview")


def test_submit_rejects_non_digest_planned_asset_metadata_drift(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    changed_asset = plan.assets[0].validated_copy(update={"media_type": MediaType.VIDEO})
    changed_plan = plan.validated_copy(update={"assets": (changed_asset,)})

    with pytest.raises(RenderJobIntegrityError, match="asset metadata differs"):
        RenderOrchestrator(library).submit(changed_plan, purpose="preview")


def test_plan_snapshot_digest_detects_tampering(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")
    plan_path = library.paths.root / str(job.payload["plan_storage_key"])
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["total_duration_seconds"] = 0.5
    payload["scenes"][0]["duration_seconds"] = 0.5
    payload["scenes"][0]["end_seconds"] = 0.5
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RenderJobIntegrityError, match="digest changed"):
        orchestrator.load_plan(job.job_id)

    assert library.database.get_job(job.job_id).state == "queued"  # type: ignore[union-attr]


def test_run_job_rejects_tampered_content_addressed_source_bytes(tmp_path: Path) -> None:
    library, project, asset_id = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")
    asset = library.database.get_asset(asset_id)
    assert asset is not None
    blob_path = library.assets.resolve(asset)
    blob_path.write_text("tampered after ingest\n", encoding="utf-8")

    with pytest.raises(RenderJobIntegrityError, match="source bytes"):
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)

    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "failed"
    assert not (
        library.paths.root / str(job.payload["command_manifest_storage_key"])
    ).exists()


def test_run_job_rejects_custom_asset_path_with_wrong_bytes(tmp_path: Path) -> None:
    library, project, asset_id = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")
    alternate = tmp_path / "alternate.ppm"
    alternate.write_text(
        "P3\n4 6\n255\n" + "\n".join(["255 255 255"] * 24) + "\n",
        encoding="ascii",
    )

    with pytest.raises(RenderJobIntegrityError, match="source bytes"):
        orchestrator.run_job(
            job.job_id,
            _capabilities(),
            asset_paths={asset_id: alternate},
            prefer_nvenc=False,
        )

    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "failed"


def test_backend_start_failure_is_persisted_without_partial_artifact(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")

    with pytest.raises(FFmpegBackendError) as caught:
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)

    assert caught.value.error.code == "ffmpeg_start_failed"
    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "failed"
    failure = orchestrator.load_failure(job.job_id)
    assert failure is not None
    assert failure.code == "ffmpeg_start_failed"
    assert failure.state == "failed"
    command_path = library.paths.root / str(job.payload["command_manifest_storage_key"])
    assert command_path.is_file()
    assert not (library.paths.root / str(job.payload["output_storage_key"])).exists()
    assert not (library.paths.root / str(job.payload["manifest_storage_key"])).exists()


def test_pre_cancelled_job_records_cancelled_terminal_state(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(FFmpegBackendError) as caught:
        orchestrator.run_job(
            job.job_id,
            _capabilities(),
            prefer_nvenc=False,
            cancellation=cancellation,
        )

    assert caught.value.error.code == "render_cancelled"
    assert library.database.get_job(job.job_id).state == "cancelled"  # type: ignore[union-attr]
    failure = orchestrator.load_failure(job.job_id)
    assert failure is not None
    assert failure.state == "cancelled"


def test_completed_or_failed_job_cannot_be_executed_twice(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(
        project,
        library.database,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")

    with pytest.raises(FFmpegBackendError):
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)
    with pytest.raises(RenderJobStateError, match="must be queued"):
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)
