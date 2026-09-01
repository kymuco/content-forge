"""PR18 panel OCR extraction and correction workflow over existing ReviewTask authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Self

from pydantic import Field, model_validator

from content_forge.core import (
    AttentionMode,
    MediaType,
    Project,
    ReviewPriority,
    ReviewStatus,
    ReviewTask,
    dump_json,
    load_json,
)
from content_forge.core.ids import EntityKind, require_entity_id
from content_forge.core.models import FrozenModel, LanguageTag, SHA256
from content_forge.providers.ocr import (
    OCRInvocationEvidence,
    OCRPixelRect,
    OCRPoint,
    OCRProvider,
    OCRRequest,
    OCRResult,
    semantic_ocr_request_digest,
)
from content_forge.storage import LocalLibrary

_PANEL_OCR_VERSION = "pr18_panel_ocr_v1"
_PANEL_OCR_METADATA_KEY = "pr18_panel_ocr"
_OCR_REVIEW_TASK = "ocr_text_correction"


class PanelOCRError(RuntimeError):
    pass


class PanelOCRConflictError(PanelOCRError):
    pass


class PanelOCRNotFoundError(PanelOCRError):
    pass


class PanelOCRValidationError(PanelOCRError):
    pass


class PanelTextRegion(FrozenModel):
    region_id: str = Field(pattern=r"^ocr_[0-9]{4}$")
    provider_index: int = Field(ge=0)
    raw_text: str = Field(min_length=1, max_length=30000)
    corrected_text: str | None = Field(default=None, min_length=1, max_length=30000)
    confidence: float = Field(ge=0.0, le=1.0)
    polygon: tuple[OCRPoint, ...] = Field(min_length=4, max_length=32)
    bbox: OCRPixelRect
    language: LanguageTag | None = None

    @property
    def effective_text(self) -> str:
        return self.raw_text if self.corrected_text is None else self.corrected_text


class PanelTextExtraction(FrozenModel):
    contract_version: str = _PANEL_OCR_VERSION
    project_id: str
    scene_id: str
    asset_id: str
    source_sha256: SHA256
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    review_confidence_threshold: float = Field(ge=0.0, le=1.0)
    regions: tuple[PanelTextRegion, ...] = Field(default=(), max_length=10000)
    evidence: OCRInvocationEvidence

    @model_validator(mode="after")
    def validate_identity_and_regions(self) -> Self:
        require_entity_id(self.project_id, EntityKind.PROJECT)
        require_entity_id(self.scene_id, EntityKind.SCENE)
        require_entity_id(self.asset_id, EntityKind.ASSET)
        ids = tuple(region.region_id for region in self.regions)
        if len(set(ids)) != len(ids):
            raise ValueError("panel OCR region IDs must be unique")
        return self

    @property
    def uncertain_region_ids(self) -> tuple[str, ...]:
        return tuple(
            region.region_id
            for region in self.regions
            if region.confidence < self.review_confidence_threshold
            and region.corrected_text is None
        )


class PanelOCRPreparation(FrozenModel):
    extraction: PanelTextExtraction
    review_task: ReviewTask | None = None


def panel_extraction_digest(extraction: PanelTextExtraction) -> str:
    encoded = json.dumps(
        extraction.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _metadata_snapshots(project: Project) -> dict[str, object]:
    value = project.metadata.get(_PANEL_OCR_METADATA_KEY, {})
    if not isinstance(value, Mapping):
        raise PanelOCRValidationError("project PR18 OCR metadata is malformed")
    return {str(key): item for key, item in value.items()}


def _load_extraction(project: Project, scene_id: str) -> PanelTextExtraction:
    raw = _metadata_snapshots(project).get(scene_id)
    if not isinstance(raw, Mapping):
        raise PanelOCRNotFoundError(f"no OCR extraction for scene {scene_id}")
    try:
        return PanelTextExtraction.model_validate(raw)
    except Exception as exc:
        raise PanelOCRValidationError("stored panel OCR extraction is malformed") from exc


def _review_task_for(extraction: PanelTextExtraction) -> ReviewTask | None:
    uncertain = extraction.uncertain_region_ids
    if not uncertain:
        return None
    by_id = {region.region_id: region for region in extraction.regions}
    payload_regions = [
        {
            "region_id": region_id,
            "raw_text": by_id[region_id].raw_text,
            "confidence": by_id[region_id].confidence,
            "bbox": by_id[region_id].bbox.model_dump(mode="json"),
        }
        for region_id in uncertain
    ]
    return ReviewTask(
        project_id=extraction.project_id,
        task_type=_OCR_REVIEW_TASK,
        attention=AttentionMode.REVIEW,
        priority=ReviewPriority.HIGH,
        blocking=True,
        payload={
            "scene_id": extraction.scene_id,
            "asset_id": extraction.asset_id,
            "extraction_digest": panel_extraction_digest(extraction),
            "uncertain_region_ids": list(uncertain),
            "regions": payload_regions,
        },
    )


def prepare_panel_ocr(
    project: Project,
    scene_id: str,
    result: OCRResult,
    *,
    review_confidence_threshold: float = 0.80,
) -> PanelOCRPreparation:
    require_entity_id(scene_id, EntityKind.SCENE)
    scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
    if scene is None:
        raise PanelOCRNotFoundError(f"unknown project scene: {scene_id}")
    if scene.media is None:
        raise PanelOCRValidationError("panel OCR scene has no media asset")

    extraction = PanelTextExtraction(
        project_id=project.project_id,
        scene_id=scene.scene_id,
        asset_id=scene.media.asset_id,
        source_sha256=result.source_sha256,
        width=result.width,
        height=result.height,
        review_confidence_threshold=review_confidence_threshold,
        regions=tuple(
            PanelTextRegion(
                region_id=region.region_id,
                provider_index=region.provider_index,
                raw_text=region.raw_text,
                confidence=region.confidence,
                polygon=region.polygon,
                bbox=region.bbox,
                language=region.language,
            )
            for region in result.regions
        ),
        evidence=result.evidence,
    )
    return PanelOCRPreparation(
        extraction=extraction,
        review_task=_review_task_for(extraction),
    )


class PanelOCRWorkflow:
    """Durable extraction/correction workflow without speaker attribution authority."""

    def __init__(self, library: LocalLibrary, provider: OCRProvider) -> None:
        self.library = library
        self.provider = provider

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise PanelOCRNotFoundError(f"unknown project: {project_id}")
        raw = str(row["manifest_json"])
        return load_json(Project, raw), raw

    def _cas_project(self, expected_json: str, updated: Project) -> Project:
        serialized = dump_json(updated)
        with self.library.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE projects
                SET content_kind = ?, state = ?, manifest_json = ?, updated_at = ?
                WHERE project_id = ? AND manifest_json = ?
                """,
                (
                    updated.content_kind,
                    updated.state.value,
                    serialized,
                    updated.updated_at.isoformat(),
                    updated.project_id,
                    expected_json,
                ),
            ).rowcount
            if changed != 1:
                raise PanelOCRConflictError(
                    f"project changed concurrently: {updated.project_id}"
                )
        return updated

    def extract_scene(
        self,
        project_id: str,
        scene_id: str,
        *,
        language_hints: tuple[LanguageTag, ...] = (),
        review_confidence_threshold: float = 0.80,
    ) -> Project:
        project, expected_json = self._snapshot(project_id)
        require_entity_id(scene_id, EntityKind.SCENE)
        scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
        if scene is None:
            raise PanelOCRNotFoundError(f"unknown project scene: {scene_id}")
        if scene.media is None:
            raise PanelOCRValidationError("panel OCR scene has no media asset")
        asset = self.library.database.get_asset(scene.media.asset_id)
        if asset is None:
            raise PanelOCRNotFoundError(f"unknown scene asset: {scene.media.asset_id}")
        if asset.media_type is not MediaType.IMAGE:
            raise PanelOCRValidationError("PR18 panel OCR requires an image asset")
        if asset.width is None or asset.height is None:
            raise PanelOCRValidationError("panel OCR requires authoritative image dimensions")
        if not self.library.assets.verify(asset):
            raise PanelOCRValidationError("panel OCR source bytes failed SHA-256 verification")

        path = self.library.assets.resolve(asset)
        request = OCRRequest(
            image_path=path,
            source_sha256=asset.sha256,
            width=asset.width,
            height=asset.height,
            language_hints=language_hints,
        )
        request_digest = semantic_ocr_request_digest(request)

        snapshots = _metadata_snapshots(project)
        if scene_id in snapshots:
            existing = _load_extraction(project, scene_id)
            if existing.asset_id != asset.asset_id or existing.source_sha256 != asset.sha256:
                raise PanelOCRConflictError(
                    "retained OCR extraction no longer matches the immutable scene asset"
                )
            if existing.review_confidence_threshold != review_confidence_threshold:
                raise PanelOCRConflictError(
                    "retained OCR extraction uses a different review confidence threshold; explicit re-OCR/review-policy migration is required"
                )
            if existing.evidence.request_sha256 != request_digest:
                raise PanelOCRConflictError(
                    "retained OCR extraction uses different semantic request hints; explicit re-OCR is required"
                )
            # PR18 v1 has no implicit re-OCR. Repeating the exact operation is an
            # idempotent read of the retained raw/corrected snapshot.
            return project

        result = self.provider.extract(request)
        if result.source_sha256 != asset.sha256:
            raise PanelOCRValidationError("OCR provider result source digest mismatch")
        if result.width != asset.width or result.height != asset.height:
            raise PanelOCRValidationError("OCR provider result source dimensions mismatch")
        if result.evidence.request_sha256 != request_digest:
            raise PanelOCRValidationError("OCR provider evidence request digest mismatch")

        prepared = prepare_panel_ocr(
            project,
            scene_id,
            result,
            review_confidence_threshold=review_confidence_threshold,
        )
        snapshots[scene_id] = prepared.extraction.model_dump(mode="json")

        tasks = list(project.review_tasks)
        existing_tasks = [
            task
            for task in tasks
            if task.task_type == _OCR_REVIEW_TASK
            and task.payload.get("scene_id") == scene_id
        ]
        if existing_tasks:
            raise PanelOCRConflictError("scene already has an OCR correction task without extraction")
        if prepared.review_task is not None:
            tasks.append(prepared.review_task)

        metadata = project.model_dump(mode="json")["metadata"]
        assert isinstance(metadata, dict)
        metadata[_PANEL_OCR_METADATA_KEY] = snapshots
        updated = project.validated_copy(
            update={
                "metadata": metadata,
                "review_tasks": tuple(tasks),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self._cas_project(expected_json, updated)

    def apply_corrections(
        self,
        project_id: str,
        review_task_id: str,
        corrections: Mapping[str, str],
    ) -> Project:
        require_entity_id(review_task_id, EntityKind.REVIEW)
        project, expected_json = self._snapshot(project_id)
        task = next(
            (item for item in project.review_tasks if item.review_task_id == review_task_id),
            None,
        )
        if task is None:
            raise PanelOCRNotFoundError(f"unknown review task: {review_task_id}")
        if task.task_type != _OCR_REVIEW_TASK:
            raise PanelOCRValidationError("review task is not an OCR correction task")
        if task.status is not ReviewStatus.OPEN or task.resolved_at is not None:
            raise PanelOCRConflictError("OCR correction task is already closed")
        scene_id = task.payload.get("scene_id")
        if not isinstance(scene_id, str):
            raise PanelOCRValidationError("OCR correction task scene identity is malformed")
        extraction = _load_extraction(project, scene_id)
        expected_digest = task.payload.get("extraction_digest")
        if expected_digest != panel_extraction_digest(extraction):
            raise PanelOCRConflictError("OCR correction task no longer matches raw extraction")

        uncertain = set(extraction.uncertain_region_ids)
        supplied = set(corrections)
        if supplied != uncertain:
            missing = sorted(uncertain - supplied)
            extra = sorted(supplied - uncertain)
            raise PanelOCRValidationError(
                f"OCR corrections must cover exactly uncertain regions; missing={missing}, extra={extra}"
            )
        normalized: dict[str, str] = {}
        for region_id, text in corrections.items():
            if not isinstance(text, str) or not text.strip():
                raise PanelOCRValidationError("OCR corrected text must be non-empty")
            cleaned = text.strip()
            if len(cleaned) > 30000:
                raise PanelOCRValidationError("OCR corrected text exceeds limit")
            normalized[region_id] = cleaned

        regions = tuple(
            region.validated_copy(
                update={"corrected_text": normalized[region.region_id]}
            )
            if region.region_id in normalized
            else region
            for region in extraction.regions
        )
        corrected = extraction.validated_copy(update={"regions": regions})
        if corrected.uncertain_region_ids:
            raise PanelOCRValidationError("OCR correction did not resolve every uncertain region")

        snapshots = _metadata_snapshots(project)
        snapshots[scene_id] = corrected.model_dump(mode="json")
        metadata = project.model_dump(mode="json")["metadata"]
        assert isinstance(metadata, dict)
        metadata[_PANEL_OCR_METADATA_KEY] = snapshots

        now = datetime.now(timezone.utc)
        resolved = task.validated_copy(
            update={
                "status": ReviewStatus.RESOLVED,
                "accepted_value": {
                    "extraction_digest": panel_extraction_digest(corrected),
                    "corrections": dict(sorted(normalized.items())),
                },
                "resolved_at": now,
            }
        )
        tasks = tuple(
            resolved if item.review_task_id == task.review_task_id else item
            for item in project.review_tasks
        )
        updated = project.validated_copy(
            update={"metadata": metadata, "review_tasks": tasks, "updated_at": now}
        )
        return self._cas_project(expected_json, updated)

    def extraction(self, project_id: str, scene_id: str) -> PanelTextExtraction:
        project, _ = self._snapshot(project_id)
        return _load_extraction(project, scene_id)


__all__ = [
    "PanelOCRConflictError",
    "PanelOCRError",
    "PanelOCRNotFoundError",
    "PanelOCRPreparation",
    "PanelOCRValidationError",
    "PanelOCRWorkflow",
    "PanelTextExtraction",
    "PanelTextRegion",
    "panel_extraction_digest",
    "prepare_panel_ocr",
]
