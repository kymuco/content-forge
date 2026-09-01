"""PR24 hardening for reversible shared-scene provenance and exact host authority."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from content_forge.core import (
    AssetRef,
    EntityKind,
    Project,
    Scene,
    SourceRecord,
    require_entity_id,
)
from content_forge.core.models import FrozenModel

from . import long_form_shared_workflow as _base
from .long_form_shared import (
    LongFormSharedSceneConflictError,
    SharedVoicedSceneRef,
    _digest,
    _select_scene_authority,
    _source_snapshot,
)

_PROVENANCE_VERSION = "pr24_shared_voiced_scene_provenance_v1"
_PROVENANCE_METADATA_KEY = "pr24_shared_voiced_scene_provenance"


class ProjectLongFormSharedProvenance(FrozenModel):
    """Exact provenance objects added by PR24 and therefore safe for PR24 to remove."""

    contract_version: Literal["pr24_shared_voiced_scene_provenance_v1"] = (
        _PROVENANCE_VERSION
    )
    project_id: str
    owned_source_refs: tuple[AssetRef, ...] = Field(default=(), max_length=10000)
    owned_source_records: tuple[SourceRecord, ...] = Field(default=(), max_length=10000)

    @model_validator(mode="after")
    def validate_ownership(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        ref_keys = tuple(
            (item.asset_id, item.source_id, item.role) for item in self.owned_source_refs
        )
        if len(set(ref_keys)) != len(ref_keys):
            raise ValueError("PR24 owned source refs must be unique")
        record_ids = tuple(item.source_id for item in self.owned_source_records)
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("PR24 owned source records must be unique")
        if any(item.role != "pr24_shared_source" for item in self.owned_source_refs):
            raise ValueError("PR24 owned source refs must use the reserved shared-source role")
        return self


def _provenance_manifest(project: Project) -> ProjectLongFormSharedProvenance | None:
    raw = project.metadata.get(_PROVENANCE_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise LongFormSharedSceneConflictError(
            "stored PR24 shared-scene provenance ownership is malformed"
        )
    try:
        manifest = ProjectLongFormSharedProvenance.model_validate(raw)
    except Exception as exc:
        raise LongFormSharedSceneConflictError(
            "stored PR24 shared-scene provenance ownership is malformed"
        ) from exc
    if manifest.project_id != project.project_id:
        raise LongFormSharedSceneConflictError(
            "PR24 shared-scene provenance project identity mismatch"
        )
    return manifest


def _all_asset_refs(project: Project) -> tuple[AssetRef, ...]:
    refs: list[AssetRef] = list(project.source_refs)
    for scene in project.scenes:
        if scene.media is not None:
            refs.append(scene.media)
        refs.extend(item.asset_ref for item in scene.overlays if item.asset_ref is not None)
        refs.extend(item.asset_ref for item in scene.audio_tracks if item.asset_ref is not None)
    refs.extend(item.asset_ref for item in project.overlays if item.asset_ref is not None)
    refs.extend(item.asset_ref for item in project.audio_tracks if item.asset_ref is not None)
    return tuple(refs)


def _require_contiguous_local_order(scenes: tuple[Scene, ...]) -> None:
    orders = tuple(scene.order for scene in sorted(scenes, key=lambda item: item.order))
    if orders != tuple(range(len(scenes))):
        raise LongFormSharedSceneConflictError(
            "long-form host local Scene order must remain contiguous before shared scenes"
        )


class LongFormSharedSceneWorkflow(_base.LongFormSharedSceneWorkflow):
    """Add exact reversible provenance ownership to the PR24 host-copy workflow."""

    @staticmethod
    def _validate_provenance(
        project: Project,
        provenance: ProjectLongFormSharedProvenance,
    ) -> None:
        for owned_ref in provenance.owned_source_refs:
            matches = tuple(item for item in project.source_refs if item == owned_ref)
            if len(matches) != 1:
                raise LongFormSharedSceneConflictError(
                    "PR24 owned shared-source ref is missing or duplicated"
                )
        records = {item.source_id: item for item in project.source_records}
        for owned_record in provenance.owned_source_records:
            if records.get(owned_record.source_id) != owned_record:
                raise LongFormSharedSceneConflictError(
                    "PR24 owned shared-source record changed after materialization"
                )

    def _base_host(
        self,
        project: Project,
        shared: _base.ProjectLongFormSharedManifest | None,
        provenance: ProjectLongFormSharedProvenance | None,
    ) -> Project:
        if shared is None:
            self._reject_orphans(project)
            if provenance is not None:
                raise LongFormSharedSceneConflictError(
                    "orphaned PR24 shared-scene provenance without shared-scene manifest"
                )
            return project
        if provenance is None:
            raise LongFormSharedSceneConflictError(
                "PR24 shared-scene manifest has no provenance ownership receipt"
            )

        self._validate_owned_state(project, shared)
        self._validate_provenance(project, provenance)
        scenes = tuple(scene for scene in project.scenes if not _base._owned_scene(scene))
        _require_contiguous_local_order(scenes)

        owned_refs = set(provenance.owned_source_refs)
        source_refs = tuple(item for item in project.source_refs if item not in owned_refs)

        candidate = project.validated_copy(
            update={"scenes": scenes, "source_refs": source_refs}
        )
        used_source_ids = {
            item.source_id
            for item in _all_asset_refs(candidate)
            if item.source_id is not None
        }
        owned_record_ids = {item.source_id for item in provenance.owned_source_records}
        source_records = tuple(
            item
            for item in project.source_records
            if item.source_id not in owned_record_ids or item.source_id in used_source_ids
        )
        metadata = dict(project.metadata)
        metadata.pop(_base._SHARED_METADATA_KEY, None)
        metadata.pop(_PROVENANCE_METADATA_KEY, None)
        return project.validated_copy(
            update={
                "scenes": scenes,
                "source_refs": source_refs,
                "source_records": source_records,
                "metadata": metadata,
            }
        )

    @staticmethod
    def _merge_provenance_with_ownership(
        host: Project,
        source_projects: tuple[Project, ...],
        source_scenes: tuple[Scene, ...],
    ) -> tuple[
        tuple[AssetRef, ...],
        tuple[SourceRecord, ...],
        ProjectLongFormSharedProvenance,
    ]:
        refs = list(host.source_refs)
        records = list(host.source_records)
        records_by_id = {item.source_id: item for item in records}
        ref_keys = {(item.asset_id, item.source_id, item.role) for item in refs}
        owned_refs: list[AssetRef] = []
        owned_records: list[SourceRecord] = []

        for source_project, source_scene in zip(source_projects, source_scenes):
            source_records = {item.source_id: item for item in source_project.source_records}
            for asset_ref in _base._scene_asset_refs(source_scene):
                if asset_ref.source_id is None:
                    continue
                record = source_records.get(asset_ref.source_id)
                if record is None or record.asset_id != asset_ref.asset_id:
                    raise LongFormSharedSceneConflictError(
                        "shared source Scene provenance is missing or inconsistent"
                    )
                existing = records_by_id.get(record.source_id)
                if existing is not None and existing != record:
                    raise LongFormSharedSceneConflictError(
                        "shared source provenance collides with host source identity"
                    )
                if existing is None:
                    records.append(record)
                    records_by_id[record.source_id] = record
                    owned_records.append(record)

                copied_ref = AssetRef(
                    asset_id=record.asset_id,
                    source_id=record.source_id,
                    role="pr24_shared_source",
                )
                key = (copied_ref.asset_id, copied_ref.source_id, copied_ref.role)
                if key not in ref_keys:
                    refs.append(copied_ref)
                    ref_keys.add(key)
                    owned_refs.append(copied_ref)

        return (
            tuple(refs),
            tuple(records),
            ProjectLongFormSharedProvenance(
                project_id=host.project_id,
                owned_source_refs=tuple(owned_refs),
                owned_source_records=tuple(owned_records),
            ),
        )

    def validate_snapshot(
        self,
        project: Project,
    ) -> _base.ProjectLongFormSharedManifest | None:
        shared = _base.long_form_shared_manifest(project)
        provenance = _provenance_manifest(project)
        if shared is None:
            self._reject_orphans(project)
            if provenance is not None:
                raise LongFormSharedSceneConflictError(
                    "orphaned PR24 shared-scene provenance without shared-scene manifest"
                )
            return None
        if provenance is None:
            raise LongFormSharedSceneConflictError(
                "PR24 shared-scene materialization has no provenance ownership receipt"
            )
        self._validate_provenance(project, provenance)
        result = super().validate_snapshot(project)
        assert result is not None
        return result

    def materialize(
        self,
        project_id: str,
        references: tuple[SharedVoicedSceneRef, ...],
    ) -> _base.ProjectLongFormSharedManifest:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _base._EDITABLE_STATES:
            raise LongFormSharedSceneConflictError(
                f"shared scenes cannot mutate host in state {project.state.value}"
            )

        previous = _base.long_form_shared_manifest(project)
        previous_provenance = _provenance_manifest(project)
        if previous is not None and tuple(
            item.reference for item in previous.bindings
        ) == references:
            self.validate_snapshot(project)
            return previous

        base = self._base_host(project, previous, previous_provenance)
        source_keys = tuple(
            (item.source_project_id, item.source_scene_id) for item in references
        )
        if len(set(source_keys)) != len(source_keys):
            raise LongFormSharedSceneConflictError("shared source scenes may not be duplicated")
        if any(item.source_project_id == project.project_id for item in references):
            raise LongFormSharedSceneConflictError(
                "PR24 shared scene references must cross a Project boundary"
            )
        _require_contiguous_local_order(base.scenes)

        source_projects: list[Project] = []
        source_scenes: list[Scene] = []
        materialized: list[Scene] = []
        bindings: list[_base.LongFormSharedSceneBinding] = []
        for index, reference in enumerate(references):
            source_project, source_manifest = _source_snapshot(
                self.library,
                reference.source_project_id,
            )
            source_scene, source_plan = _select_scene_authority(
                source_project,
                source_manifest,
                reference.source_scene_id,
            )
            if (
                _digest(source_scene) != reference.source_scene_sha256
                or source_plan.pr22_scene_sha256 != reference.pr22_scene_sha256
                or _digest(source_plan) != reference.pr23_scene_plan_sha256
            ):
                raise LongFormSharedSceneConflictError(
                    "shared scene reference no longer matches current source authority"
                )
            if _base._owned_scene(source_scene):
                raise LongFormSharedSceneConflictError(
                    "transitive PR24 shared-scene references are not supported"
                )
            copied = _base._materialized_scene(
                project.project_id,
                reference,
                source_scene,
                order=len(base.scenes) + index,
            )
            materialized.append(copied)
            bindings.append(
                _base.LongFormSharedSceneBinding(
                    reference=reference,
                    materialized_scene_id=copied.scene_id,
                    materialized_scene_sha256=_digest(copied),
                )
            )
            source_projects.append(source_project)
            source_scenes.append(source_scene)

        all_scene_ids = {scene.scene_id for scene in base.scenes}
        all_overlay_ids = {item.overlay_id for item in base.overlays}
        all_audio_ids = {item.audio_track_id for item in base.audio_tracks}
        for scene in base.scenes:
            all_overlay_ids.update(item.overlay_id for item in scene.overlays)
            all_audio_ids.update(item.audio_track_id for item in scene.audio_tracks)
        for scene in materialized:
            if scene.scene_id in all_scene_ids:
                raise LongFormSharedSceneConflictError("PR24 deterministic Scene ID collision")
            overlay_ids = {item.overlay_id for item in scene.overlays}
            audio_ids = {item.audio_track_id for item in scene.audio_tracks}
            if all_overlay_ids.intersection(overlay_ids):
                raise LongFormSharedSceneConflictError("PR24 deterministic Overlay ID collision")
            if all_audio_ids.intersection(audio_ids):
                raise LongFormSharedSceneConflictError(
                    "PR24 deterministic AudioTrack ID collision"
                )
            all_scene_ids.add(scene.scene_id)
            all_overlay_ids.update(overlay_ids)
            all_audio_ids.update(audio_ids)

        source_refs, source_records, provenance = self._merge_provenance_with_ownership(
            base,
            tuple(source_projects),
            tuple(source_scenes),
        )
        shared = _base.ProjectLongFormSharedManifest(
            project_id=project.project_id,
            bindings=tuple(bindings),
        )
        metadata = dict(base.metadata)
        metadata[_base._SHARED_METADATA_KEY] = shared.model_dump(mode="json")
        metadata[_PROVENANCE_METADATA_KEY] = provenance.model_dump(mode="json")
        updated = base.validated_copy(
            update={
                "scenes": (*base.scenes, *materialized),
                "source_refs": source_refs,
                "source_records": source_records,
                "metadata": metadata,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._cas(expected_json, updated)
        return shared

    def dematerialize(self, project_id: str) -> bool:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _base._EDITABLE_STATES:
            raise LongFormSharedSceneConflictError(
                f"shared scenes cannot mutate host in state {project.state.value}"
            )
        shared = _base.long_form_shared_manifest(project)
        provenance = _provenance_manifest(project)
        if shared is None:
            self._reject_orphans(project)
            if provenance is not None:
                raise LongFormSharedSceneConflictError(
                    "orphaned PR24 shared-scene provenance without shared-scene manifest"
                )
            return False
        base = self._base_host(project, shared, provenance)
        updated = base.validated_copy(update={"updated_at": datetime.now(timezone.utc)})
        self._cas(expected_json, updated)
        return True


__all__ = [
    "LongFormSharedSceneWorkflow",
    "ProjectLongFormSharedProvenance",
]
