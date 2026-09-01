"""PR18 panel OCR extraction and correction workflow over existing ReviewTask authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal, Self

from pydantic import Field, model_validator

from content_forge.core import (
    AttentionMode,
    MediaType,
    Project,
    ProjectState,
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
_OCR_RESUME_STATE_KEY = "pr18_ocr_resume_state"
_OCR_REVIEW_TASK = "ocr_text_correction"
_MAX_PANEL_REGIONS = 2048
_MAX_PANEL_RAW_TEXT_CHARS = 1_000_000
_MAX_REVIEW_REGIONS = 256
_MAX_REVIEW_RAW_TEXT_CHARS = 250_000
_MAX_CORRECTION_TEXT_CHARS = 250_000
_EDITABLE_OCR_STATES = frozenset(
    {
        ProjectState.INBOX,
        ProjectState.DRAFT,
        ProjectState.PREPARED,
        ProjectState.NEEDS_REVIEW,
        ProjectState.READY,
    }
)


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
    contract_version: Literal["pr18_panel_ocr_v1"] = _PANEL_OCR_VERSION
    project_id: str
    scene_id: str
    asset_id: str
    source_sha256: SHA256
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    review_confidence_threshold: float = Field(ge=0.0, le=1.0)
    regions: tuple[PanelTextRegion, ...] = Field(default=(), max_length=_MAX_PANEL_REGIONS)
    evidence: OCRInvocationEvidence

    @model_validator(mode="after")
    def validate_identity_and_regions(self) -> Self:
        require_entity_id(self.project_id, EntityKind.PROJECT)
        require_entity_id(self.scene_id, EntityKind.SCENE)
        require_entity_id(self.asset_id, EntityKind.ASSET)
        ids = tuple(region.region_id for region in self.regions)
        if len(set(ids)) != len(ids):
            raise ValueError("panel OCR region IDs must be unique")
        if sum(len(region.raw_text) for region in self.regions) > _MAX_PANEL_RAW_TEXT_CHARS:
            raise ValueError("panel OCR raw text exceeds manifest budget")
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


def _plain_metadata(project: Project) -> dict[str, object]:
    metadata = project.model_dump(mode="json")["metadata"]
    if not isinstance(metadata, dict):  # pragma: no cover - Project guarantees JSON object
        raise PanelOCRValidationError("project metadata is malformed")
    return metadata


def _metadata_snapshots(project: Project) -> dict[str, object]:
    value = _plain_metadata(project).get(_PANEL_OCR_METADATA_KEY, {})
    if not isinstance(value, dict):
        raise PanelOCRValidationError("project PR18 OCR metadata is malformed")
    return dict(value)


def _load_extraction(project: Project, scene_id: str) -> PanelTextExtraction:
    raw = _metadata_snapshots(project).get(scene_id)
    if not isinstance(raw, Mapping):
        raise PanelOCRNotFoundError(f"no OCR extraction for scene {scene_id}")
    try:
        return PanelTextExtraction.model_validate(raw)
    except Exception as exc:
        raise PanelOCRValidationError("stored panel OCR extraction is malformed") from exc


def _validated_resume_state(value: object, *, label: str) -> ProjectState:
    try:
        state = ProjectState(value)
    except (TypeError, ValueError) as exc:
        raise PanelOCRValidationError(f"{label} is malformed") from exc
    if state not in _EDITABLE_OCR_STATES:
        raise PanelOCRValidationError(f"{label} is not editable")
    return state


def _review_payload_for(
    extraction: PanelTextExtraction,
    *,
    resume_state: ProjectState,
) -> dict[str, object] | None:
    uncertain = extraction.uncertain_region_ids
    if not uncertain:
        return None
    if len(uncertain) > _MAX_REVIEW_REGIONS:
        raise PanelOCRValidationError("uncertain OCR region count exceeds review-task budget")
    by_id = {region.region_id: region for region in extraction.regions}
    if sum(len(by_id[region_id].raw_text) for region_id in uncertain) > _MAX_REVIEW_RAW_TEXT_CHARS:
        raise PanelOCRValidationError("uncertain OCR raw text exceeds review-task budget")
    payload_regions = [
        {
            "region_id": region_id,
            "raw_text": by_id[region_id].raw_text,
            "confidence": by_id[region_id].confidence,
            "bbox": by_id[region_id].bbox.model_dump(mode="json"),
        }
        for region_id in uncertain
    ]
    return {
        "scene_id": extraction.scene_id,
        "asset_id": extraction.asset_id,
        "resume_state": resume_state.value,
        "extraction_digest": panel_extraction_digest(extraction),
        "uncertain_region_ids": list(uncertain),
        "regions": payload_regions,
    }


def _review_task_for(
    extraction: PanelTextExtraction,
    *,
    resume_state: ProjectState,
) -> ReviewTask | None:
    payload = _review_payload_for(extraction, resume_state=resume_state)
    if payload is None:
        return None
    return ReviewTask(
        project_id=extraction.project_id,
        task_type=_OCR_REVIEW_TASK,
        attention=AttentionMode.REVIEW,
        priority=ReviewPriority.HIGH,
        blocking=True,
        payload=payload,
    )


def prepare_panel_ocr(
    project: Project,
    scene_id: str,
    result: OCRResult,
    *,
    review_confidence_threshold: float = 0.80,
    resume_state: ProjectState | None = None,
) -> PanelOCRPreparation:
    require_entity_id(scene_id, EntityKind.SCENE)
    if project.state not in _EDITABLE_OCR_STATES:
        raise PanelOCRConflictError(
            f"panel OCR cannot mutate project in state {project.state.value}"
        )
    resolved_resume_state = project.state if resume_state is None else resume_state
    if resolved_resume_state not in _EDITABLE_OCR_STATES:
        raise PanelOCRValidationError("panel OCR resume state is not editable")
    scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
    if scene is None:
        raise PanelOCRNotFoundError(f"unknown project scene: {scene_id}")
    if scene.media is None:
        raise PanelOCRValidationError("panel OCR scene has no media asset")
    if len(result.regions) > _MAX_PANEL_REGIONS:
        raise PanelOCRValidationError("OCR region count exceeds panel manifest budget")
    if sum(len(region.raw_text) for region in result.regions) > _MAX_PANEL_RAW_TEXT_CHARS:
        raise PanelOCRValidationError("OCR raw text exceeds panel manifest budget")

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
        review_task=_review_task_for(extraction, resume_state=resolved_resume_state),
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

    def _shared_resume_state(self, project: Project) -> ProjectState:
        raw = project.metadata.get(_OCR_RESUME_STATE_KEY)
        if raw is None:
            return project.state
        if project.state is not ProjectState.NEEDS_REVIEW:
            raise PanelOCRConflictError(
                "OCR review checkpoint exists outside project state needs_review"
            )
        return _validated_resume_state(raw, label="project OCR resume state")

    @staticmethod
    def _assert_extraction_identity(
        extraction: PanelTextExtraction,
        *,
        project: Project,
        scene_id: str,
        asset_id: str,
        source_sha256: str,
        width: int,
        height: int,
    ) -> None:
        if extraction.project_id != project.project_id or extraction.scene_id != scene_id:
            raise PanelOCRConflictError("retained OCR extraction identity does not match project scene")
        if extraction.asset_id != asset_id or extraction.source_sha256 != source_sha256:
            raise PanelOCRConflictError("retained OCR extraction no longer matches the immutable scene asset")
        if extraction.width != width or extraction.height != height:
            raise PanelOCRConflictError("retained OCR extraction dimensions no longer match scene asset")

    def extract_scene(
        self,
        project_id: str,
        scene_id: str,
        *,
        language_hints: tuple[LanguageTag, ...] = (),
        review_confidence_threshold: float = 0.80,
    ) -> Project:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_OCR_STATES:
            raise PanelOCRConflictError(
                f"panel OCR cannot mutate project in state {project.state.value}"
            )
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
            self._assert_extraction_identity(
                existing,
                project=project,
                scene_id=scene_id,
                asset_id=asset.asset_id,
                source_sha256=asset.sha256,
                width=asset.width,
                height=asset.height,
            )
            if existing.review_confidence_threshold != review_confidence_threshold:
                raise PanelOCRConflictError(
                    "retained OCR extraction uses a different review confidence threshold; explicit re-OCR/review-policy migration is required"
                )
            if existing.evidence.request_sha256 != request_digest:
                raise PanelOCRConflictError(
                    "retained OCR extraction uses different semantic request hints; explicit re-OCR is required"
                )
            return project

        shared_resume_state = self._shared_resume_state(project)
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
            resume_state=shared_resume_state,
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

        metadata = _plain_metadata(project)
        metadata[_PANEL_OCR_METADATA_KEY] = snapshots
        if prepared.review_task is not None:
            existing_resume = metadata.get(_OCR_RESUME_STATE_KEY)
            if existing_resume is not None and existing_resume != shared_resume_state.value:
                raise PanelOCRConflictError("project OCR review checkpoint changed unexpectedly")
            metadata[_OCR_RESUME_STATE_KEY] = shared_resume_state.value
        updated = project.validated_copy(
            update={
                "state": (
                    ProjectState.NEEDS_REVIEW
                    if prepared.review_task is not None
                    else project.state
                ),
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
        if project.state not in _EDITABLE_OCR_STATES:
            raise PanelOCRConflictError(
                f"panel OCR cannot mutate project in state {project.state.value}"
            )
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
        if (
            task.attention is not AttentionMode.REVIEW
            or task.priority is not ReviewPriority.HIGH
            or not task.blocking
            or task.accepted_value is not None
        ):
            raise PanelOCRValidationError("OCR correction task authority is malformed")
        if project.state is not ProjectState.NEEDS_REVIEW:
            raise PanelOCRConflictError(
                "open OCR correction task requires project state needs_review"
            )
        checkpoint_resume_state = _validated_resume_state(
            project.metadata.get(_OCR_RESUME_STATE_KEY),
            label="project OCR resume state",
        )
        task_resume_state = _validated_resume_state(
            task.payload.get("resume_state"),
            label="OCR correction task resume state",
        )
        if checkpoint_resume_state is not task_resume_state:
            raise PanelOCRConflictError("OCR correction task resume state does not match project checkpoint")

        scene_id = task.payload.get("scene_id")
        if not isinstance(scene_id, str):
            raise PanelOCRValidationError("OCR correction task scene identity is malformed")
        try:
            require_entity_id(scene_id, EntityKind.SCENE)
        except ValueError as exc:
            raise PanelOCRValidationError("OCR correction task scene identity is malformed") from exc
        extraction = _load_extraction(project, scene_id)
        scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
        if scene is None or scene.media is None:
            raise PanelOCRConflictError("OCR correction scene no longer has its source asset")
        task_asset_id = task.payload.get("asset_id")
        if not isinstance(task_asset_id, str) or task_asset_id != scene.media.asset_id:
            raise PanelOCRConflictError("OCR correction task asset identity no longer matches scene")
        asset = self.library.database.get_asset(scene.media.asset_id)
        if asset is None or asset.width is None or asset.height is None:
            raise PanelOCRConflictError("OCR correction source asset metadata is unavailable")
        self._assert_extraction_identity(
            extraction,
            project=project,
            scene_id=scene_id,
            asset_id=asset.asset_id,
            source_sha256=asset.sha256,
            width=asset.width,
            height=asset.height,
        )
        expected_digest = task.payload.get("extraction_digest")
        if expected_digest != panel_extraction_digest(extraction):
            raise PanelOCRConflictError("OCR correction task no longer matches raw extraction")
        canonical_payload = _review_payload_for(
            extraction,
            resume_state=checkpoint_resume_state,
        )
        task_payload = task.model_dump(mode="json")["payload"]
        if canonical_payload is None or task_payload != canonical_payload:
            raise PanelOCRConflictError(
                "OCR correction task payload no longer matches retained extraction"
            )

        uncertain = set(extraction.uncertain_region_ids)
        supplied = set(corrections)
        if supplied != uncertain:
            missing = sorted(uncertain - supplied)
            extra = sorted(supplied - uncertain)
            raise PanelOCRValidationError(
                f"OCR corrections must cover exactly uncertain regions; missing={missing}, extra={extra}"
            )
        normalized: dict[str, str] = {}
        correction_chars = 0
        for region_id, text in corrections.items():
            if not isinstance(text, str) or not text.strip():
                raise PanelOCRValidationError("OCR corrected text must be non-empty")
            cleaned = text.strip()
            if len(cleaned) > 30000:
                raise PanelOCRValidationError("OCR corrected text exceeds per-region limit")
            correction_chars += len(cleaned)
            if correction_chars > _MAX_CORRECTION_TEXT_CHARS:
                raise PanelOCRValidationError("OCR corrections exceed accepted-text budget")
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
        metadata = _plain_metadata(project)
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
        remaining_blocking = any(
            item.status is ReviewStatus.OPEN and item.blocking for item in tasks
        )
        remaining_ocr = any(
            item.status is ReviewStatus.OPEN and item.task_type == _OCR_REVIEW_TASK
            for item in tasks
        )
        if not remaining_ocr:
            metadata.pop(_OCR_RESUME_STATE_KEY, None)
        next_state = (
            ProjectState.NEEDS_REVIEW
            if remaining_blocking
            else checkpoint_resume_state
        )
        updated = project.validated_copy(
            update={
                "state": next_state,
                "metadata": metadata,
                "review_tasks": tasks,
                "updated_at": now,
            }
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
