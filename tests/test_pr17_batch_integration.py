from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from content_forge.batch import BatchCoordinator, BatchRenderInput, ExportSidecar
from content_forge.core import AssetRef, MediaType, Project, Scene, TemplateRef, Variant
from content_forge.profiles import shorts_final_profile, shorts_preview_profile
from content_forge.render.ffmpeg import probe_ffmpeg_runtime
from content_forge.storage import LocalLibrary
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    compile_hook_overlay,
)
from content_forge.variants import localized_variant_snapshot


def test_batch_preview_and_final_render_qc_export_end_to_end(tmp_path: Path) -> None:
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
    variant = Variant(language="en", hook="Batch preview and final stay reproducible")
    project = Project(
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        variants=(variant,),
        scenes=(
            Scene(
                order=0,
                duration_seconds=0.35,
                media=AssetRef(asset_id=ingest.asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(), shorts_final_profile()),
    )
    library.save_project(project)
    preview = compile_hook_overlay(
        project,
        library.database,
        profile_id="shorts_preview",
        variant_id=variant.variant_id,
    )
    final = compile_hook_overlay(
        project,
        library.database,
        profile_id="shorts_final",
        variant_id=variant.variant_id,
    )
    localized = localized_variant_snapshot(variant)
    coordinator = BatchCoordinator(library)
    parent = coordinator.prepare(
        [
            BatchRenderInput(
                plan=preview,
                purpose="preview",
                localized_variant=localized,
            ),
            BatchRenderInput(
                plan=final,
                purpose="final",
                localized_variant=localized,
            ),
        ]
    )

    result = coordinator.run_batch(
        parent.job_id,
        capabilities,
        prefer_nvenc=False,
        render_timeout=30.0,
        qc_timeout=30.0,
    )

    assert result.status == "succeeded"
    assert len(result.items) == 2
    assert all(item.state == "succeeded" and item.qc_passed for item in result.items)
    stored_parent = library.database.get_job(parent.job_id)
    assert stored_parent is not None and stored_parent.state == "succeeded"
    assert isinstance(stored_parent.payload.get("batch_result_digest"), str)
    assert coordinator.load_result(parent.job_id) == result

    manifest = coordinator.load_manifest(parent.job_id)
    by_key = {item.item_key: item for item in manifest.items}
    for item_result in result.items:
        assert item_result.export_sidecar_storage_key is not None
        export_path = library.paths.root / item_result.export_sidecar_storage_key
        export = ExportSidecar.model_validate_json(export_path.read_text(encoding="utf-8"))
        frozen = by_key[item_result.item_key]
        assert export.render_plan_digest == frozen.render_plan_digest
        assert export.source_assets == frozen.source_assets
        assert export.localized_variant_digest is not None
        assert export.renderer_backend_version == "1"
        assert export.output_sha256
        checks = {check.name: check for check in export.qc_report.checks}
        assert checks["dimensions"].status == "pass"
        assert checks["duration"].status == "pass"
        assert checks["source_assets"].status == "pass"
        assert checks["audio_presence"].status == "pass"
        assert checks["text_overflow"].status == "pass"
        assert checks["safe_zones"].status == "pass"
        assert checks["black_frames"].status == "pass"
