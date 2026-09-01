from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from content_forge.application import PanelTextExtraction
from content_forge.providers import (
    OCRInvocationEvidence,
    OCRRequest,
    PaddleOCRProvider,
)


class _Result:
    def __init__(self, payload):
        self.json = {"res": payload}


class _Runtime:
    def predict(self, _path: str):
        return [
            _Result(
                {
                    "rec_texts": ["  preserved text  "],
                    "rec_scores": [0.9],
                    "rec_polys": [[[1, 1], [20, 1], [20, 10], [1, 10]]],
                    "rec_boxes": [[1, 1, 20, 10]],
                }
            )
        ]


def test_ocr_evidence_rejects_unknown_contract_version() -> None:
    with pytest.raises(ValidationError):
        OCRInvocationEvidence(
            contract_version="pr99_unknown",
            provider_id="fake",
            provider_version="1",
            model_id="fake",
            request_sha256="a" * 64,
            config_sha256="b" * 64,
        )


def test_panel_extraction_rejects_unknown_contract_version() -> None:
    with pytest.raises(ValidationError):
        PanelTextExtraction.model_validate(
            {
                "contract_version": "pr99_unknown",
                "project_id": "cf_project_" + "1" * 32,
                "scene_id": "cf_scene_" + "2" * 32,
                "asset_id": "cf_asset_" + "3" * 32,
                "source_sha256": "a" * 64,
                "width": 100,
                "height": 100,
                "review_confidence_threshold": 0.8,
                "regions": [],
                "evidence": {
                    "provider_id": "fake",
                    "provider_version": "1",
                    "model_id": "fake",
                    "request_sha256": "b" * 64,
                    "config_sha256": "c" * 64,
                },
            }
        )


def test_paddle_adapter_preserves_nonblank_raw_text_verbatim(tmp_path: Path) -> None:
    provider = PaddleOCRProvider(
        runtime_factory=lambda _config: _Runtime(),
        provider_version="3.7.0",
    )
    result = provider.extract(
        OCRRequest(
            image_path=tmp_path / "panel.png",
            source_sha256="d" * 64,
            width=100,
            height=100,
        )
    )
    assert result.regions[0].raw_text == "  preserved text  "
