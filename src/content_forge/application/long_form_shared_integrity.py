"""Final PR24 shared-scene cleanup integrity over reversible provenance ownership."""

from __future__ import annotations

from content_forge.core import AssetRef, Project

from . import long_form_shared_hardening as _hardening
from . import long_form_shared_workflow as _base
from .long_form_shared import LongFormSharedSceneConflictError


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


class LongFormSharedSceneWorkflow(_hardening.LongFormSharedSceneWorkflow):
    """Remove PR24 provenance atomically without constructing an invalid interim Project."""

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


ProjectLongFormSharedProvenance = _hardening.ProjectLongFormSharedProvenance


__all__ = [
    "LongFormSharedSceneWorkflow",
    "ProjectLongFormSharedProvenance",
]
