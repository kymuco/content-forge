"""Optional local PaddleOCR 3.x adapter for the PR18 OCR contract."""

from __future__ import annotations

import importlib.metadata
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import Field

from content_forge.core.models import FrozenModel

from .ocr import (
    OCRExecutionError,
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRProviderHealth,
    OCRRegion,
    OCRRequest,
    OCRResponseError,
    OCRResult,
    OCRUnavailableError,
    ocr_config_digest,
    semantic_ocr_request_digest,
)

_PROVIDER_ID = "paddleocr_local"


class PaddleOCRConfig(FrozenModel):
    """Portable local-inference intent; engine installation remains environment-specific."""

    ocr_version: str = Field(default="PP-OCRv6", min_length=1, max_length=64)
    lang: str | None = Field(default=None, min_length=1, max_length=64)
    engine: str = Field(default="paddle", min_length=1, max_length=64)
    device: str | None = Field(default=None, min_length=1, max_length=64)
    use_textline_orientation: bool = False


RuntimeFactory = Callable[[PaddleOCRConfig], Any]


def _installed_version() -> str:
    try:
        return importlib.metadata.version("paddleocr")
    except importlib.metadata.PackageNotFoundError as exc:
        raise OCRUnavailableError(
            "PaddleOCR is not installed; install a compatible local OCR environment"
        ) from exc


def _default_runtime_factory(config: PaddleOCRConfig):
    version = _installed_version()
    major = version.split(".", 1)[0]
    if major != "3":
        raise OCRUnavailableError(
            f"unsupported PaddleOCR major version {version!r}; PR18 expects 3.x"
        )
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # pragma: no cover - environment-specific optional dependency
        raise OCRUnavailableError("PaddleOCR import failed") from exc

    kwargs: dict[str, object] = {
        "ocr_version": config.ocr_version,
        "engine": config.engine,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": config.use_textline_orientation,
    }
    if config.lang is not None:
        kwargs["lang"] = config.lang
    if config.device is not None:
        kwargs["device"] = config.device
    try:
        return PaddleOCR(**kwargs)
    except Exception as exc:  # pragma: no cover - model/engine availability is local
        raise OCRUnavailableError("PaddleOCR runtime initialization failed") from exc


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    candidate = getattr(value, "json", None)
    if callable(candidate):
        candidate = candidate()
    if isinstance(candidate, Mapping):
        return candidate
    raise OCRResponseError("PaddleOCR result does not expose a mapping/json payload")


def _finite_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OCRResponseError("PaddleOCR recognition score is not numeric")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise OCRResponseError("PaddleOCR recognition score is outside [0, 1]")
    return score


def _points(value: object) -> tuple[OCRPoint, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise OCRResponseError("PaddleOCR polygon is not a coordinate sequence")
    result: list[OCRPoint] = []
    for pair in value:
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes, bytearray)):
            raise OCRResponseError("PaddleOCR polygon point is malformed")
        if len(pair) != 2:
            raise OCRResponseError("PaddleOCR polygon point must contain x/y")
        x, y = pair
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
        ):
            raise OCRResponseError("PaddleOCR polygon coordinates must be numeric")
        xf, yf = float(x), float(y)
        if not math.isfinite(xf) or not math.isfinite(yf) or xf < 0.0 or yf < 0.0:
            raise OCRResponseError("PaddleOCR polygon contains invalid coordinates")
        result.append(OCRPoint(x=xf, y=yf))
    if len(result) < 4:
        raise OCRResponseError("PaddleOCR polygon must contain at least four points")
    return tuple(result)


def _bbox(value: object | None, polygon: tuple[OCRPoint, ...]) -> OCRPixelRect:
    if value is not None:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise OCRResponseError("PaddleOCR bbox is not a coordinate sequence")
        if len(value) != 4:
            raise OCRResponseError("PaddleOCR bbox must contain four coordinates")
        coordinates: list[float] = []
        for coordinate in value:
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
                raise OCRResponseError("PaddleOCR bbox coordinates must be numeric")
            number = float(coordinate)
            if not math.isfinite(number) or number < 0.0:
                raise OCRResponseError("PaddleOCR bbox contains invalid coordinates")
            coordinates.append(number)
        return OCRPixelRect(
            x_min=coordinates[0],
            y_min=coordinates[1],
            x_max=coordinates[2],
            y_max=coordinates[3],
        )
    xs = [point.x for point in polygon]
    ys = [point.y for point in polygon]
    return OCRPixelRect(x_min=min(xs), y_min=min(ys), x_max=max(xs), y_max=max(ys))


class PaddleOCRProvider:
    """Lazy local adapter. No PaddleOCR dependency is imported by base Content Forge."""

    def __init__(
        self,
        config: PaddleOCRConfig | None = None,
        *,
        runtime_factory: RuntimeFactory | None = None,
        provider_version: str | None = None,
    ) -> None:
        self.config = config or PaddleOCRConfig()
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._runtime = None
        self._provider_version_override = provider_version

    def _provider_version(self) -> str:
        if self._provider_version_override is not None:
            return self._provider_version_override
        return _installed_version()

    def _get_runtime(self):
        if self._runtime is None:
            self._runtime = self._runtime_factory(self.config)
        return self._runtime

    def health(self) -> OCRProviderHealth:
        try:
            version = self._provider_version()
            self._get_runtime()
        except OCRUnavailableError as exc:
            return OCRProviderHealth(
                provider_id=_PROVIDER_ID,
                provider_version=self._provider_version_override or "unavailable",
                available=False,
                reason=str(exc),
            )
        except Exception as exc:
            return OCRProviderHealth(
                provider_id=_PROVIDER_ID,
                provider_version=self._provider_version_override or "unknown",
                available=False,
                reason=f"PaddleOCR health check failed: {type(exc).__name__}",
            )
        return OCRProviderHealth(
            provider_id=_PROVIDER_ID,
            provider_version=version,
            available=True,
        )

    def extract(self, request: OCRRequest) -> OCRResult:
        try:
            runtime = self._get_runtime()
            prediction = runtime.predict(str(request.image_path))
        except OCRUnavailableError:
            raise
        except Exception as exc:
            raise OCRExecutionError("PaddleOCR prediction failed") from exc

        try:
            items = list(prediction)
        except Exception as exc:
            raise OCRResponseError("PaddleOCR prediction is not iterable") from exc
        if len(items) != 1:
            raise OCRResponseError("PR18 image OCR expects exactly one PaddleOCR result")

        payload = _as_mapping(items[0])
        result_payload = payload.get("res", payload)
        if not isinstance(result_payload, Mapping):
            raise OCRResponseError("PaddleOCR result payload is malformed")

        texts = result_payload.get("rec_texts")
        scores = result_payload.get("rec_scores")
        polygons = result_payload.get("rec_polys")
        boxes = result_payload.get("rec_boxes")
        for label, value in (
            ("rec_texts", texts),
            ("rec_scores", scores),
            ("rec_polys", polygons),
        ):
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
                raise OCRResponseError(f"PaddleOCR {label} is missing or malformed")
        assert isinstance(texts, Sequence)
        assert isinstance(scores, Sequence)
        assert isinstance(polygons, Sequence)
        if len(texts) != len(scores) or len(texts) != len(polygons):
            raise OCRResponseError("PaddleOCR recognition arrays have different lengths")
        if boxes is not None:
            if not isinstance(boxes, Sequence) or isinstance(boxes, (str, bytes, bytearray)):
                raise OCRResponseError("PaddleOCR rec_boxes is malformed")
            if len(boxes) != len(texts):
                raise OCRResponseError("PaddleOCR rec_boxes length does not match text results")

        regions: list[OCRRegion] = []
        for provider_index, raw in enumerate(texts):
            if not isinstance(raw, str):
                raise OCRResponseError("PaddleOCR recognized text must be a string")
            raw_text = raw.strip()
            if not raw_text:
                continue
            polygon = _points(polygons[provider_index])
            box_value = None if boxes is None else boxes[provider_index]
            region = OCRRegion(
                region_id=f"ocr_{len(regions):04d}",
                provider_index=provider_index,
                raw_text=raw_text,
                confidence=_finite_score(scores[provider_index]),
                polygon=polygon,
                bbox=_bbox(box_value, polygon),
            )
            regions.append(region)

        config_json = self.config.model_dump(mode="json")
        evidence = OCRInvocationEvidence(
            provider_id=_PROVIDER_ID,
            provider_version=self._provider_version(),
            model_id=self.config.ocr_version,
            engine=self.config.engine,
            request_sha256=semantic_ocr_request_digest(request),
            config_sha256=ocr_config_digest(config_json),
        )
        try:
            return OCRResult(
                source_sha256=request.source_sha256,
                width=request.width,
                height=request.height,
                regions=tuple(regions),
                evidence=evidence,
            )
        except Exception as exc:
            raise OCRResponseError("PaddleOCR result violates source geometry contract") from exc


__all__ = ["PaddleOCRConfig", "PaddleOCRProvider"]
