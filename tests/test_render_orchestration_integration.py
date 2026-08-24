from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from content_forge.core import AssetRef, MediaType, Project, Scene, TemplateRef, Variant
from content_forge.orchestration import RenderJobIntegrityError, RenderOrchestrator
from content_forge.profiles import shorts_preview_profile
from content_forge.render.ffmpeg import probe_ffmpeg_runtime
from content_forge.storage import LocalLibrary, sha256_file
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    compile_hook_overlay,
)


def test_persisted_preview_job_renders_and_reloads_artifact_manifest(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg runtime is not installed")

    capabilities = probe_ffmpeg_runtime(test_nvenc=False)
    if not capabilities.has_libx264:
        pytest.skip("integration fixture requires libx264 CPU fallback")

    library = LocalLibrary(tmp_path / "runtime")
    source = tmp_path / "source.ppm"
    pixels: list[str] = []
    for y in range(48):
        for x in range(32):
            pixels.append("235 70 80" if (x + y) % 2 else "35 105 225")
    source.write_text(
        "P3\n32 48\n255\n" + "\n".join(pixels) + "\n",
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
        variants=(Variant(language="en", hook="Persistent render job works"),),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.45,
                media=AssetRef(asset_id=ingest.asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
    )
    library.save_project(project)
    plan = compile_hook_overlay(project, library.database)
    orchestrator = RenderOrchestrator(library)

    job = orchestrator.submit(plan, purpose="preview")
    artifact = orchestrator.run_job(
        job.job_id,
        capabilities,
        prefer_nvenc=False,
        timeout=20,
    )

    stored = library.database.get_job(job.job_id)
    assert stored is not None
    assert stored.state == "succeeded"
    assert artifact.project_id == project.project_id
    assert artifact.profile_id == "shorts_preview"
    assert artifact.width == 540
    assert artifact.height == 960
    assert artifact.bytes_written > 0
    assert artifact.video_encoder == "libx264"
    assert artifact.source_assets[0].asset_id == ingest.asset.asset_id
    assert artifact.source_assets[0].sha256 == ingest.asset.sha256

    output_path = library.paths.root / artifact.output_storage_key
    command_path = library.paths.root / artifact.command_manifest_storage_key
    manifest_path = library.paths.root / artifact.manifest_storage_key
    assert output_path.is_file()
    assert command_path.is_file()
    assert manifest_path.is_file()
    assert sha256_file(output_path) == artifact.output_sha256

    command_payload = json.loads(command_path.read_text(encoding="utf-8"))
    assert command_payload["render_plan_digest"] == artifact.render_plan_digest
    assert command_payload["video_encoder"] == artifact.video_encoder

    reloaded = orchestrator.load_artifact(job.job_id)
    assert reloaded == artifact
    assert orchestrator.load_failure(job.job_id) is None

    command_payload["video_encoder"] = "tampered_encoder"
    command_path.write_text(json.dumps(command_payload), encoding="utf-8")
    with pytest.raises(RenderJobIntegrityError, match="command-manifest digest"):
        orchestrator.load_artifact(job.job_id)
