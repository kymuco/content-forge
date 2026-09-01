from __future__ import annotations

import pytest

from content_forge.application import PanelOCRValidationError, prepare_panel_ocr
from content_forge.core import AssetRef, Project, Scene
from content_forge.providers import (
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRRegion,
    OCRResult,
)


def _region(index: int) -> OCRRegion:
    y = float(index % 100)
    return OCRRegion(
        region_id=f"ocr_{index:04d}",
        provider_index=index,
        raw_text="x",
        confidence=0.10,
        polygon=(
            OCRPoint(x=1, y=y),
            OCRPoint(x=2, y=y),
            OCRPoint(x=2, y=y + 1),
            OCRPoint(x=1, y=y + 1),
        ),
        bbox=OCRPixelRect(x_min=1, y_min=y, x_max=2, y_max=y + 1),
    )


def test_uncertain_region_count_fails_before_giant_review_task() -> None:
    asset_id = "cf_asset_" + "1" * 32
    scene = Scene(
        scene_id="cf_scene_" + "2" * 32,
        order=0,
        duration_seconds=1.0,
        media=AssetRef(asset_id=asset_id),
    )
    project = Project(
        project_id="cf_project_" + "3" * 32,
        content_kind="panel_sequence",
        source_refs=(AssetRef(asset_id=asset_id),),
        scenes=(scene,),
    )
    result = OCRResult(
        source_sha256="a" * 64,
        width=100,
        height=200,
        regions=tuple(_region(index) for index in range(257)),
        evidence=OCRInvocationEvidence(
            provider_id="fake",
            provider_version="1",
            model_id="fake",
            request_sha256="b" * 64,
            config_sha256="c" * 64,
        ),
    )

    with pytest.raises(PanelOCRValidationError, match="review-task budget"):
        prepare_panel_ocr(project, scene.scene_id, result)
