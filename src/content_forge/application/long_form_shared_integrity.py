"""Final PR24 shared-scene authority and reversible provenance integrity."""

from __future__ import annotations

from datetime import datetime, timezone

from content_forge.core import AssetRef, Project, Scene

from . import long_form_shared_hardening as _hardening
from . import long_form_shared_workflow as _base
from .long_form_shared import (
    LongFormSharedSceneConflictError,
    SharedVoicedSceneRef,
    _digest,
    _source_snapshot,
    _validate_reference_authority,
)


def _remaining_refs(
    project: Project,
    *,
    source_refs: tuple[AssetRef, ...],
    keep_local_scenes: bool,
) -> tuple[AssetRef, ...]:
    refs: list[AssetRef] = list(source_refs)
    scenes = (
        tuple(scene for scene in project.scenes if not _base._owned_scene(scene))
        if keep_local_scenes
        else project.scenes
    )
    for scene in scenes:
        if scene.media is not None:
            refs.append(scene.media)
        refs.extend(item.asset_ref for item in scene.overlays if item.asset_ref is not None)
        refs.extend(item.asset_ref for item in scene.audio_tracks if item.asset_ref is not None)
    refs.extend(item.asset_ref for item in project.overlays if item.asset_ref is not None)
    refs.extend(item.asset_ref for item in project.audio_tracks if item.asset_ref is not None)
    return tuple(refs)


def _materialized_scene(
    host_project_id: str,
    reference: SharedVoicedSceneRef,
    source: Scene,
    *,
    order: int,
) -> Scene:
    if any(str(key).startswith("pr24_") for key in source.properties):
        raise LongFormSharedSceneConflictError(
            "shared source Scene already occupies the reserved PR24 property namespace"
        )
    copied = _base._materialized_scene(
        host_project_id,
        reference,
        source,
        order=order,
    )
    properties = dict(copied.properties)
    properties["pr24_source_provenance_sha256"] = reference.source_provenance_sha256
    return copied.validated_copy(update={"properties": properties})


class LongFormSharedSceneWorkflow(_hardening.LongFormSharedSceneWorkflow):
    """Public PR24 authority: exact source validation plus reversible host ownership."""

    def _base_host(
        self,
        project: Project,
        shared: _base.ProjectLongFormSharedManifest | None,
        provenance: _hardening.ProjectLongFormSharedProvenance | None,
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
        _hardening._require_contiguous_local_order(scenes)

        owned_refs = set(provenance.owned_source_refs)
        source_refs = tuple(item for item in project.source_refs if item not in owned_refs)
        used_source_ids = {
            item.source_id
            for item in _remaining_refs(
                project,
                source_refs=source_refs,
                keep_local_scenes=True,
            )
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
        metadata.pop(_hardening._PROVENANCE_METADATA_KEY, None)
        return project.validated_copy(
            update={
                "scenes": scenes,
                "source_refs": source_refs,
                "source_records": source_records,
                "metadata": metadata,
            }
        )

    def validate_snapshot(
        self,
        project: Project,
    ) -> _base.ProjectLongFormSharedManifest | None:
        shared = _base.long_form_shared_manifest(project)
        provenance = _hardening._provenance_manifest(project)
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

        self._validate_owned_state(project, shared)
        self._validate_provenance(project, provenance)
        local_scenes = tuple(scene for scene in project.scenes if not _base._owned_scene(scene))
        _hardening._require_contiguous_local_order(local_scenes)
        base_order = len(local_scenes)
        for index, binding in enumerate(shared.bindings):
            source_project, source_manifest = _source_snapshot(
                self.library,
                binding.reference.source_project_id,
            )
            source_scene, _ = _validate_reference_authority(
                source_project,
                source_manifest,
                binding.reference,
            )
            if _base._owned_scene(source_scene):
                raise LongFormSharedSceneConflictError(
                    "transitive PR24 shared-scene references are not supported"
                )
            expected = _materialized_scene(
                project.project_id,
                binding.reference,
                source_scene,
                order=base_order + index,
            )
            if (
                expected.scene_id != binding.materialized_scene_id
                or _digest(expected) != binding.materialized_scene_sha256
            ):
                raise LongFormSharedSceneConflictError(
                    "PR24 host copy no longer matches exact shared source authority"
                )
        return shared

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
        if not references:
            raise LongFormSharedSceneConflictError(
                "shared-scene materialization requires at least one reference; use dematerialize to remove it"
            )

        previous = _base.long_form_shared_manifest(project)
        previous_provenance = _hardening._provenance_manifest(project)
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
        _hardening._require_contiguous_local_order(base.scenes)

        source_projects: list[Project] = []
        source_scenes: list[Scene] = []
        materialized: list[Scene] = []
        bindings: list[_base.LongFormSharedSceneBinding] = []
        for index, reference in enumerate(references):
            source_project, source_manifest = _source_snapshot(
                self.library,
                reference.source_project_id,
            )
            source_scene, _ = _validate_reference_authority(
                source_project,
                source_manifest,
                reference,
            )
            if _base._owned_scene(source_scene):
                raise LongFormSharedSceneConflictError(
                    "transitive PR24 shared-scene references are not supported"
                )
            copied = _materialized_scene(
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
        metadata[_hardening._PROVENANCE_METADATA_KEY] = provenance.model_dump(mode="json")
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
        provenance = _hardening._provenance_manifest(project)
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


ProjectLongFormSharedProvenance = _hardening.ProjectLongFormSharedProvenance


__all__ = [
    "LongFormSharedSceneWorkflow",
    "ProjectLongFormSharedProvenance",
]
