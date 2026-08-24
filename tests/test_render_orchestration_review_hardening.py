from __future__ import annotations

import json
from pathlib import Path

import pytest

import content_forge.orchestration._render_jobs_hardened as hardened
from content_forge.core import (
    AssetRef,
    MediaType,
    Project,
    Scene,
    SourceRecord,
    TemplateRef,
    Variant,
)
from content_forge.orchestration import RenderJobIntegrityError, RenderOrchestrator
from content_forge.profiles import shorts_preview_profile
from content_forge.render.ffmpeg import FFmpegBackendError, FFmpegCapabilities
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
        variants=(Variant(language="en", hook="Review hardening hook"),),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.4,
                media=AssetRef(asset_id=ingest.asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
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


def test_submit_rejects_variant_language_drift(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(project, library.database)
    changed_plan = plan.validated_copy(update={"variant_language": "fr"})

    with pytest.raises(RenderJobIntegrityError, match="variant ID/language"):
        RenderOrchestrator(library).submit(changed_plan, purpose="preview")


def test_submit_rejects_source_not_declared_by_project(tmp_path: Path) -> None:
    library, project, asset_id = _fixture(tmp_path)
    plan = compile_hook_overlay(project, library.database)

    unrelated_project_source = SourceRecord(
        asset_id=asset_id,
        platform="synthetic-review-fixture",
    )
    library.database.add_source(unrelated_project_source)
    changed_scene = plan.scenes[0].validated_copy(
        update={"media_source_id": unrelated_project_source.source_id}
    )
    changed_plan = plan.validated_copy(update={"scenes": (changed_scene,)})

    with pytest.raises(RenderJobIntegrityError, match="not declared by the stored project"):
        RenderOrchestrator(library).submit(changed_plan, purpose="preview")


def test_cleanup_error_cannot_strand_claimed_job_in_running(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(project, library.database)
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")

    manifest_path = library.paths.root / str(job.payload["manifest_storage_key"])
    manifest_path.mkdir(parents=True)

    with pytest.raises(FFmpegBackendError) as caught:
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)

    assert caught.value.error.code == "ffmpeg_start_failed"
    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "failed"
    failure = orchestrator.load_failure(job.job_id)
    assert failure is not None
    assert failure.code == "ffmpeg_start_failed"


def test_failure_manifest_tamper_is_rejected_by_sqlite_receipt(tmp_path: Path) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(project, library.database)
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")

    with pytest.raises(FFmpegBackendError):
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)

    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert isinstance(stored.payload.get("command_manifest_digest"), str)
    assert isinstance(stored.payload.get("failure_manifest_digest"), str)

    failure_path = library.paths.root / str(job.payload["failure_storage_key"])
    payload = json.loads(failure_path.read_text(encoding="utf-8"))
    payload["message"] = "tampered diagnostic evidence"
    failure_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RenderJobIntegrityError, match="authoritative job receipt"):
        orchestrator.load_failure(job.job_id)


def test_command_receipt_requires_successful_manifest_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, project, _ = _fixture(tmp_path)
    plan = compile_hook_overlay(project, library.database)
    orchestrator = RenderOrchestrator(library)
    job = orchestrator.submit(plan, purpose="preview")

    original_write = hardened._atomic_write_model

    def reject_command_manifest(path: Path, model: object) -> None:
        if path.name == "command-manifest.json":
            raise OSError("synthetic command publication failure")
        original_write(path, model)

    monkeypatch.setattr(hardened, "_atomic_write_model", reject_command_manifest)

    with pytest.raises(OSError, match="synthetic command publication failure"):
        orchestrator.run_job(job.job_id, _capabilities(), prefer_nvenc=False)

    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "failed"
    assert "command_manifest_digest" not in stored.payload
    assert isinstance(stored.payload.get("failure_manifest_digest"), str)
    assert orchestrator.load_failure(job.job_id) is not None
