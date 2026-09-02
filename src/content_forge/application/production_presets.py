"""PR32 human-facing production presets over existing template authority."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from content_forge.core import MediaType, Project, ProjectState, Scene, TemplateRef, Variant
from content_forge.profiles.shorts import shorts_final_profile, shorts_preview_profile
from content_forge.storage import LocalLibrary
from content_forge.templates import (
    ART_STORY_TEMPLATE_ID,
    CONTENT_FRAME_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    HOOK_TOPBAR_TEMPLATE_ID,
    INITIAL_TEMPLATE_VERSION,
    PANEL_SEQUENCE_TEMPLATE_ID,
)

from .idempotency import normalize_idempotency_key


class ProductionPresetError(RuntimeError):
    """Base error for PR32 preset preparation."""


class ProductionPresetNotFoundError(ProductionPresetError):
    pass


class ProductionPresetValidationError(ProductionPresetError):
    pass


class ProductionPresetConflictError(ProductionPresetError):
    pass


@dataclass(frozen=True, slots=True)
class ProductionPreset:
    preset_id: str
    label: str
    description: str
    template_id: str
    template_version: str
    min_sources: int
    max_sources: int
    image_only: bool = False
    requires_hook: bool = False
    review_crop: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "preset_id": self.preset_id,
            "label": self.label,
            "description": self.description,
            "template": {
                "template_id": self.template_id,
                "version": self.template_version,
            },
            "min_sources": self.min_sources,
            "max_sources": self.max_sources,
            "image_only": self.image_only,
            "requires_hook": self.requires_hook,
        }


PRESETS = (
    ProductionPreset(
        preset_id="hook_short",
        label="Hook Short",
        description="Full-screen clip or image sequence with a reviewed hook at the top.",
        template_id=HOOK_OVERLAY_TEMPLATE_ID,
        template_version=HOOK_OVERLAY_TEMPLATE_VERSION,
        min_sources=1,
        max_sources=16,
        requires_hook=True,
        review_crop=True,
    ),
    ProductionPreset(
        preset_id="top_bar_short",
        label="Top Bar Short",
        description="Dedicated top text area with source media below it.",
        template_id=HOOK_TOPBAR_TEMPLATE_ID,
        template_version=INITIAL_TEMPLATE_VERSION,
        min_sources=1,
        max_sources=16,
        requires_hook=True,
        review_crop=True,
    ),
    ProductionPreset(
        preset_id="framed_clip",
        label="Framed Clip",
        description="Clip or image sequence placed safely inside a reusable vertical frame.",
        template_id=CONTENT_FRAME_TEMPLATE_ID,
        template_version=INITIAL_TEMPLATE_VERSION,
        min_sources=1,
        max_sources=16,
    ),
    ProductionPreset(
        preset_id="art_story",
        label="Art Story",
        description="Ordered still images with contain geometry and retained source credits.",
        template_id=ART_STORY_TEMPLATE_ID,
        template_version=INITIAL_TEMPLATE_VERSION,
        min_sources=1,
        max_sources=32,
        image_only=True,
    ),
    ProductionPreset(
        preset_id="panel_story",
        label="Panel Story",
        description="Ordered comic, manga, or manhwa panels using project scene timing.",
        template_id=PANEL_SEQUENCE_TEMPLATE_ID,
        template_version=INITIAL_TEMPLATE_VERSION,
        min_sources=1,
        max_sources=64,
        image_only=True,
    ),
)

_PRESET_BY_ID = {item.preset_id: item for item in PRESETS}
_REQUEST_LOCKS = tuple(threading.Lock() for _ in range(128))


def production_project_id(request_id: str) -> str:
    normalized = normalize_idempotency_key(request_id)
    digest = hashlib.sha256(
        f"content-forge-production-project-v1\0{normalized}".encode("utf-8")
    ).hexdigest()
    return f"cf_project_{digest[:32]}"


def _lock_for_request(request_id: str) -> threading.Lock:
    digest = hashlib.sha256(
        f"content-forge-production-project-lock-v1\0{request_id}".encode("utf-8")
    ).digest()
    return _REQUEST_LOCKS[int.from_bytes(digest[:4], "big") % len(_REQUEST_LOCKS)]


def _preset_evidence(project: Project) -> dict[str, object] | None:
    """Read nested evidence from the model's JSON form, not immutable internal containers."""

    payload = project.model_dump(mode="json")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ProductionPresetConflictError("project metadata is malformed")
    raw = metadata.get("production_preset_v1")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProductionPresetConflictError("production preset evidence is malformed")
    return raw


def preset_for_id(preset_id: str) -> ProductionPreset:
    try:
        return _PRESET_BY_ID[preset_id]
    except KeyError as exc:
        raise ProductionPresetNotFoundError(f"unknown production preset: {preset_id}") from exc


def preset_for_project(project: Project) -> ProductionPreset | None:
    raw = _preset_evidence(project)
    if raw is None:
        return None
    if raw.get("schema_version") != 1:
        raise ProductionPresetConflictError("production preset evidence version is unsupported")
    preset_id = raw.get("preset_id")
    template_id = raw.get("template_id")
    template_version = raw.get("template_version")
    request_id = raw.get("request_id")
    source_project_ids = raw.get("source_project_ids")
    if not all(
        isinstance(value, str)
        for value in (preset_id, template_id, template_version, request_id)
    ):
        raise ProductionPresetConflictError("production preset evidence is malformed")
    try:
        normalized_request = normalize_idempotency_key(request_id)
    except ValueError as exc:
        raise ProductionPresetConflictError("production preset request identity is malformed") from exc
    if normalized_request != request_id:
        raise ProductionPresetConflictError("production preset request identity is not canonical")
    if (
        not isinstance(source_project_ids, list)
        or not source_project_ids
        or len(source_project_ids) > 64
        or not all(isinstance(value, str) and value for value in source_project_ids)
        or len(set(source_project_ids)) != len(source_project_ids)
    ):
        raise ProductionPresetConflictError("production preset source identity is malformed")
    if project.project_id != production_project_id(request_id):
        raise ProductionPresetConflictError("production project ID does not match request identity")

    preset = preset_for_id(preset_id)
    if not preset.min_sources <= len(source_project_ids) <= preset.max_sources:
        raise ProductionPresetConflictError("production preset source count is outside its contract")
    if (preset.template_id, preset.template_version) != (template_id, template_version):
        raise ProductionPresetConflictError("production preset evidence changed template identity")
    if project.template != TemplateRef(
        template_id=preset.template_id,
        version=preset.template_version,
    ):
        raise ProductionPresetConflictError("project template does not match production preset evidence")
    return preset


class ProductionPresetService:
    """Create exact production Projects from explicitly selected Inbox source projects."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library

    @staticmethod
    def list_presets() -> tuple[ProductionPreset, ...]:
        return PRESETS

    def _source(self, project_id: str):
        project = self.library.load_project(project_id)
        if project is None:
            raise ProductionPresetValidationError(f"unknown source project: {project_id}")
        if project.project_id != project_id:
            raise ProductionPresetConflictError("Inbox source project identity is inconsistent")
        if not isinstance(project.metadata.get("inbox_intake_id"), str):
            raise ProductionPresetValidationError(
                "production sources must be original Inbox projects"
            )
        if len(project.source_refs) != 1:
            raise ProductionPresetValidationError(
                f"Inbox source project must contain exactly one source asset: {project_id}"
            )
        ref = project.source_refs[0]
        asset = self.library.database.get_asset(ref.asset_id)
        if asset is None:
            raise ProductionPresetConflictError(
                f"source project references missing asset: {project_id}"
            )
        if asset.media_type not in {MediaType.IMAGE, MediaType.VIDEO}:
            raise ProductionPresetValidationError(
                f"source is not prepared image/video media: {project_id}"
            )
        if asset.media_type is MediaType.VIDEO:
            if asset.duration_seconds is None or asset.duration_seconds <= 0:
                raise ProductionPresetValidationError(
                    f"source video has no usable duration: {project_id}"
                )
            if asset.has_audio is None:
                raise ProductionPresetValidationError(
                    f"source video has incomplete authoritative probe metadata: {project_id}"
                )
        records = tuple(
            record
            for record in project.source_records
            if ref.source_id is None or record.source_id == ref.source_id
        )
        if ref.source_id is not None and not records:
            raise ProductionPresetConflictError(
                f"source provenance is missing from Inbox project: {project_id}"
            )
        return project, ref, asset, records

    @staticmethod
    def _validate_source_for_preset(preset: ProductionPreset, records) -> None:
        if preset.template_id != ART_STORY_TEMPLATE_ID:
            return
        for record in records:
            if record.requires_credit is True and (
                record.credit_text is None or not record.credit_text.strip()
            ):
                raise ProductionPresetValidationError(
                    "Art Story source requires credit text before project creation"
                )

    def list_sources(self, *, limit: int = 100) -> tuple[dict[str, object], ...]:
        if limit < 1 or limit > 500:
            raise ProductionPresetValidationError("source limit must be between 1 and 500")
        with self.library.database.connection() as connection:
            rows = connection.execute(
                "SELECT project_id FROM projects ORDER BY updated_at DESC, project_id"
            ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            project_id = str(row["project_id"])
            try:
                project, _ref, asset, _records = self._source(project_id)
            except (ProductionPresetError, TypeError, ValueError):
                continue
            label = project.metadata.get("original_filename")
            if not isinstance(label, str) or not label.strip():
                label = f"{asset.media_type.value.title()} source"
            items.append(
                {
                    "source_project_id": project.project_id,
                    "asset_id": asset.asset_id,
                    "label": label.strip(),
                    "media_type": asset.media_type.value,
                    "duration_seconds": asset.duration_seconds,
                    "thumbnail_endpoint": f"assets/{asset.asset_id}/thumbnail",
                    "created_at": project.created_at.isoformat(),
                }
            )
            if len(items) >= limit:
                break
        return tuple(items)

    def list_projects(self, *, limit: int = 100) -> tuple[Project, ...]:
        if limit < 1 or limit > 500:
            raise ProductionPresetValidationError("project limit must be between 1 and 500")
        with self.library.database.connection() as connection:
            rows = connection.execute(
                "SELECT project_id FROM projects ORDER BY updated_at DESC, project_id"
            ).fetchall()
        projects: list[Project] = []
        for row in rows:
            row_project_id = str(row["project_id"])
            try:
                project = self.library.load_project(row_project_id)
            except (TypeError, ValueError):
                continue
            if project is None or project.project_id != row_project_id:
                continue
            try:
                preset = preset_for_project(project)
            except ProductionPresetError:
                continue
            if preset is not None:
                projects.append(project)
                if len(projects) >= limit:
                    break
        return tuple(projects)

    def create_project(
        self,
        *,
        request_id: str,
        preset_id: str,
        source_project_ids: tuple[str, ...],
    ) -> Project:
        normalized_request = normalize_idempotency_key(request_id)
        preset = preset_for_id(preset_id)
        if len(source_project_ids) < preset.min_sources or len(source_project_ids) > preset.max_sources:
            raise ProductionPresetValidationError(
                f"{preset.label} requires {preset.min_sources}..{preset.max_sources} selected source(s)"
            )
        if len(set(source_project_ids)) != len(source_project_ids):
            raise ProductionPresetValidationError("source project selection contains duplicates")

        project_id = production_project_id(normalized_request)
        evidence = {
            "schema_version": 1,
            "request_id": normalized_request,
            "preset_id": preset.preset_id,
            "template_id": preset.template_id,
            "template_version": preset.template_version,
            "source_project_ids": list(source_project_ids),
        }

        with _lock_for_request(normalized_request):
            existing = self.library.load_project(project_id)
            if existing is not None:
                if _preset_evidence(existing) != evidence:
                    raise ProductionPresetConflictError(
                        "production request ID was reused with different preset/source input"
                    )
                preset_for_project(existing)
                return existing

            source_refs = []
            source_records = []
            scenes = []
            seen_assets: set[str] = set()
            seen_records: set[str] = set()
            for order, source_project_id in enumerate(source_project_ids):
                _project, ref, asset, records = self._source(source_project_id)
                self._validate_source_for_preset(preset, records)
                if asset.asset_id in seen_assets:
                    raise ProductionPresetValidationError(
                        "the same media asset cannot be selected twice in one production project"
                    )
                if preset.image_only and asset.media_type is not MediaType.IMAGE:
                    raise ProductionPresetValidationError(
                        f"{preset.label} accepts images only"
                    )
                seen_assets.add(asset.asset_id)
                source_refs.append(ref)
                for record in records:
                    if record.source_id not in seen_records:
                        seen_records.add(record.source_id)
                        source_records.append(record)
                duration = 5.0 if asset.media_type is MediaType.IMAGE else float(asset.duration_seconds)
                scenes.append(Scene(order=order, duration_seconds=duration, media=ref))

            now = datetime.now(timezone.utc)
            project = Project(
                project_id=project_id,
                content_kind="unclassified",
                state=ProjectState.INBOX,
                source_refs=tuple(source_refs),
                source_records=tuple(source_records),
                scenes=tuple(scenes),
                template=TemplateRef(
                    template_id=preset.template_id,
                    version=preset.template_version,
                ),
                variants=(Variant(),),
                output_profiles=(shorts_preview_profile(), shorts_final_profile()),
                metadata={"production_preset_v1": evidence},
                created_at=now,
                updated_at=now,
            )
            self.library.save_project(project)
            return project


__all__ = [
    "PRESETS",
    "ProductionPreset",
    "ProductionPresetConflictError",
    "ProductionPresetError",
    "ProductionPresetNotFoundError",
    "ProductionPresetService",
    "ProductionPresetValidationError",
    "preset_for_id",
    "preset_for_project",
    "production_project_id",
]
