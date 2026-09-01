from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from content_forge.core import AssetRef, MediaType, Project, Scene
from content_forge.orchestration import (
    RenderOrchestrator,
    RenderReuseIntegrityError,
    find_reusable_render_artifact,
)
from content_forge.profiles import (
    LONG_FORM_1080P_PROFILE_ID,
    LONG_FORM_1440P_PROFILE_ID,
    long_form_1080p_profile,
    long_form_1440p_profile,
)
from content_forge.render.ffmpeg import probe_ffmpeg_runtime
from content_forge.storage import LocalLibrary
from content_forge.timeline import compile_timeline, render_plan_digest


def test_pr24_horizontal_profiles_render_through_existing_persistent_pipeline(
    tmp_path: Path,
) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg runtime is not installed")

    capabilities = probe_ffmpeg_runtime(test_nvenc=False)
    if not capabilities.has_libx264:
        pytest.skip("integration fixture requires libx264 CPU fallback")

    library = LocalLibrary(tmp_path / "runtime")
    source = tmp_path / "wide.ppm"
    pixels: list[str] = []
    for y in range(18):
        for x in range(32):
            pixels.append("30 140 220" if (x + y) % 2 else "225 100 45")
    source.write_text(
        "P3\n32 18\n255\n" + "\n".join(pixels) + "\n",
        encoding="ascii",
    )
    ingest = library.assets.ingest_file(
        source,
        media_type=MediaType.IMAGE,
        mime_type="image/x-portable-pixmap",
    )
    ref = AssetRef(asset_id=ingest.asset.asset_id)
    project = Project(
        content_kind="long_form_fixture",
        scenes=(
            Scene(order=0, duration_seconds=0.10, media=ref),
            Scene(order=1, duration_seconds=0.10, media=ref),
        ),
        output_profiles=(
            long_form_1080p_profile(),
            long_form_1440p_profile(),
        ),
    )
    library.save_project(project)

    plan_1080 = compile_timeline(
        project,
        library.database,
        profile_id=LONG_FORM_1080P_PROFILE_ID,
    )
    plan_1440 = compile_timeline(
        project,
        library.database,
        profile_id=LONG_FORM_1440P_PROFILE_ID,
    )
    assert plan_1080.total_duration_seconds == pytest.approx(0.20)
    assert tuple(scene.start_seconds for scene in plan_1080.scenes) == (0.0, 0.1)
    assert tuple(scene.end_seconds for scene in plan_1080.scenes) == (0.1, 0.2)
    assert tuple(scene.start_seconds for scene in plan_1440.scenes) == (0.0, 0.1)
    assert render_plan_digest(plan_1080) != render_plan_digest(plan_1440)
    assert render_plan_digest(plan_1080) == render_plan_digest(
        compile_timeline(
            project,
            library.database,
            profile_id=LONG_FORM_1080P_PROFILE_ID,
        )
    )

    orchestrator = RenderOrchestrator(library)
    artifacts = []
    for plan in (plan_1080, plan_1440):
        job = orchestrator.submit(plan, purpose="final")
        artifacts.append(
            orchestrator.run_job(
                job.job_id,
                capabilities,
                prefer_nvenc=False,
                timeout=45,
            )
        )

    artifact_1080, artifact_1440 = artifacts
    assert (artifact_1080.width, artifact_1080.height) == (1920, 1080)
    assert (artifact_1440.width, artifact_1440.height) == (2560, 1440)
    assert artifact_1080.profile_id == LONG_FORM_1080P_PROFILE_ID
    assert artifact_1440.profile_id == LONG_FORM_1440P_PROFILE_ID
    assert artifact_1080.render_plan_digest == render_plan_digest(plan_1080)
    assert artifact_1440.render_plan_digest == render_plan_digest(plan_1440)
    assert artifact_1080.render_plan_digest != artifact_1440.render_plan_digest
    assert artifact_1080.source_assets[0].sha256 == ingest.asset.sha256
    assert artifact_1440.source_assets[0].sha256 == ingest.asset.sha256
    assert orchestrator.load_artifact(artifact_1080.job_id) == artifact_1080
    assert orchestrator.load_artifact(artifact_1440.job_id) == artifact_1440

    # Long-form caching reuses only a fully authenticated existing PR7 render attempt.
    assert (
        find_reusable_render_artifact(library, plan_1080, purpose="final")
        == artifact_1080
    )
    assert (
        find_reusable_render_artifact(library, plan_1440, purpose="final")
        == artifact_1440
    )

    # A matching SQLite identity is not enough. If the prior output is changed after
    # success, reuse fails closed instead of silently accepting or skipping the candidate.
    output_1080 = library.paths.root / artifact_1080.output_storage_key
    output_1080.write_bytes(output_1080.read_bytes() + b"tampered")
    with pytest.raises(RenderReuseIntegrityError, match="integrity verification"):
        find_reusable_render_artifact(library, plan_1080, purpose="final")
