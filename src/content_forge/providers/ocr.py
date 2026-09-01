"""PR18 local OCR provider contracts and deterministic region evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from content_forge.core.models import FrozenModel, LanguageTag, SHA256

_OCR_CONTRACT_VERSION = "pr18_ocr_contract_v1"


class OCRProviderError(RuntimeError):
    """Base class for optional OCR provider failures."""


class OCRUnavailableError(OCRProviderError):
    """The optional OCR package/model/runtime is unavailable."""


class OCRExecutionError(OCRProviderError):
    """OCR execution failed before a validated Content Forge result existed."""


class OCRResponseError(OCRProviderError):
    """Provider output was malformed or violated the PR18 contract."""


class OCRProviderHealth(FrozenModel):
    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    available: bool
    reason: str | None = Field(default=None, max_length=4096)


class OCRPoint(FrozenModel):
    x: float = Field(ge=0.0)
    y: float = Field(ge=0.0)


class OCRPixelRect(FrozenModel):
    x_min: float = Field(ge=0.0)
    y_min: float = Field(ge=0.0)
    x_max: float = Field(gt=0.0)
    y_max: float = Field(gt=0.0)

    @model_validator(mode="after")
    def ordered(self):
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("OCR rectangle must have positive width and height")
        return self


class OCRRequest(FrozenModel):
    """Runtime request whose semantic identity is independent of its local path."""

    image_path: Path
    source_sha256: SHA256
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    language_hints: tuple[LanguageTag, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique_languages(self):
        folded = tuple(value.casefold() for value in self.language_hints)
        if len(set(folded)) != len(folded):
            raise ValueError("OCR language_hints must be unique")
        return self


class OCRInvocationEvidence(FrozenModel):
    contract_version: str = _OCR_CONTRACT_VERSION
    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    engine: str | None = Field(default=None, max_length=128)
    request_sha256: SHA256
    config_sha256: SHA256


class OCRRegion(FrozenModel):
    region_id: str = Field(pattern=r"^ocr_[0-9]{4}$")
    provider_index: int = Field(ge=0)
    raw_text: str = Field(min_length=1, max_length=30000)
    confidence: float = Field(ge=0.0, le=1.0)
    polygon: tuple[OCRPoint, ...] = Field(min_length=4, max_length=32)
    bbox: OCRPixelRect
    language: LanguageTag | None = None

    @model_validator(mode="after")
    def geometry_is_inside_bbox(self):
        epsilon = 1e-6
        for point in self.polygon:
            if not (
                self.bbox.x_min - epsilon <= point.x <= self.bbox.x_max + epsilon
                and self.bbox.y_min - epsilon <= point.y <= self.bbox.y_max + epsilon
            ):
                raise ValueError("OCR polygon point lies outside bbox")
        return self


class OCRResult(FrozenModel):
    source_sha256: SHA256
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    regions: tuple[OCRRegion, ...] = Field(default=(), max_length=10000)
    evidence: OCRInvocationEvidence

    @model_validator(mode="after")
    def validate_regions(self):
        ids = tuple(region.region_id for region in self.regions)
        if len(set(ids)) != len(ids):
            raise ValueError("OCR region IDs must be unique")
        indexes = tuple(region.provider_index for region in self.regions)
        if len(set(indexes)) != len(indexes):
            raise ValueError("OCR provider indexes must be unique")
        epsilon = 1e-6
        for region in self.regions:
            if region.bbox.x_max > self.width + epsilon:
                raise ValueError("OCR region exceeds source width")
            if region.bbox.y_max > self.height + epsilon:
                raise ValueError("OCR region exceeds source height")
        return self


@runtime_checkable
class OCRProvider(Protocol):
    """Narrow OCR interface. Speaker identity and reading order are out of scope."""

    def health(self) -> OCRProviderHealth: ...

    def extract(self, request: OCRRequest) -> OCRResult: ...


def semantic_ocr_request_digest(request: OCRRequest) -> str:
    """Hash source identity and semantic OCR hints, deliberately excluding local path."""

    payload = {
        "contract_version": _OCR_CONTRACT_VERSION,
        "source_sha256": request.source_sha256,
        "width": request.width,
        "height": request.height,
        "language_hints": list(request.language_hints),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ocr_config_digest(config: dict[str, object]) -> str:
    encoded = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "OCRExecutionError",
    "OCRInvocationEvidence",
    "OCRPixelRect",
    "OCRPoint",
    "OCRProvider",
    "OCRProviderError",
    "OCRProviderHealth",
    "OCRRegion",
    "OCRRequest",
    "OCRResponseError",
    "OCRResult",
    "OCRUnavailableError",
    "ocr_config_digest",
    "semantic_ocr_request_digest",
]
