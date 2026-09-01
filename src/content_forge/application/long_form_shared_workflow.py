"""PR24 reversible host materialization for exact shared voiced-scene references."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from content_forge.core import (
    AssetRef,
    AudioTrack,
    EntityKind,
    MotionSpec,
    Overlay,
    Project,
    ProjectState,
    Scene,
    SourceRecord,
    dump_json,
    load_json,
    require_entity_id,
)
from content_forge.core.models import FrozenModel, SHA256
from content_forge.storage import LocalLibrary

from .long_form_shared import (
    LongFormSharedSceneConflictError,
    SharedVoicedSceneRef,
    _digest,
    _select_scene_authority,
    _source_snapshot,
)
from .voiced_scene_hardening import VoicedSceneWorkflow

_SHARED_BINDING_VERSION = "pr24_shared_voiced_scene_binding_v1"
_SHARED_MANIFEST_VERSION = "pr24_shared_voiced_scene_manifest_v1"
_SHARED_METADATA_KEY = "pr24_shared_voiced_scenes"
_SHARED_OWNER = "pr24_shared_voiced_scene_v1"
_MAX_SHARED_SCENES = 10000
_EDITABLE_STATES = frozenset(
    {
        ProjectState.DRAFT,
        ProjectState.PREPARED,
        ProjectState.NEEDS_REVIEW,
        ProjectState.READY,
    }
)


class LongFormSharedSceneBinding(FrozenModel):
    contract_version: Literal["pr24_shared_voiced_scene_binding_v1"] = (
        _SHARED_BINDING_VERSION
    )
    reference: SharedVoicedSceneRef
    materialized_scene_id: str
    materialized_scene_sha256: SHA256

    @model_validator(mode="after")
    def validate_scene_id(self):
        require_entity_id(self.materialized_scene_id, EntityKind.SCENE)
        return self


class ProjectLongFormSharedManifest(FrozenModel):
    contract_version: Literal["pr24_shared_voiced_scene_manifest_v1"] = (
        _SHARED_MANIFEST_VERSION
    )
    project_id: str
    bindings: tuple[LongFormSharedSceneBinding, ...] = Field(
        default=(),
        max_length=_MAX_SHARED_SCENES,
    )

    @model_validator(mode="after")
    def validate_manifest(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        source_keys = tuple(
            (item.reference.source_project_id, item.reference.source_scene_id)
            for item in self.bindings
        )
        if len(set(source_keys)) != len(source_keys):
            raise ValueError("shared voiced-scene source references must be unique")
        materialized_ids = tuple(item.materialized_scene_id for item in self.bindings)
        if len(set(materialized_ids)) != len(materialized_ids):
            raise ValueError("shared voiced-scene materialized IDs must be unique")
        return self


def long_form_shared_manifest(project: Project) -> ProjectLongFormSharedManifest | None:
    raw = project.metadata.get(_SHARED_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise LongFormSharedSceneConflictError("stored PR24 shared-scene metadata is malformed")
    try:
        manifest = ProjectLongFormSharedManifest.model_validate(raw)
    except Exception as exc:
        raise LongFormSharedSceneConflictError(
            "stored PR24 shared-scene manifest is malformed"
        ) from exc
    if manifest.project_id != project.project_id:
        raise LongFormSharedSceneConflictError("PR24 shared-scene project identity mismatch")
    return manifest


def _owned_scene(scene: Scene) -> bool:
    return scene.properties.get("pr24_owner") == _SHARED_OWNER


def _owned_id(kind: str, host_project_id: str, reference: SharedVoicedSceneRef, source_id: str) -> str:
    encoded = "|".join(
        (
            _SHARED_OWNER,
            kind,
            host_project_id,
            reference.source_project_id,
            reference.source_scene_id,
            source_id,
        )
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _clean_properties(properties: Mapping[str, object]) -> dict[str, object]:
    result = dict(properties)
    result.pop("pr22_owner", None)
    result.pop("pr23_owner", None)
    return result


def _copy_overlay(
    host_project_id: str,
    reference: SharedVoicedSceneRef,
    overlay: Overlay,
) -> Overlay:
    return overlay.validated_copy(
        update={
            "overlay_id": f"cf_overlay_{_owned_id('overlay', host_project_id, reference, overlay.overlay_id)}",
            "properties": _clean_properties(overlay.properties),
        }
    )


def _copy_track(
    host_project_id: str,
    reference: SharedVoicedSceneRef,
    track: AudioTrack,
) -> AudioTrack:
    return track.validated_copy(
        update={
            "audio_track_id": f"cf_audio_{_owned_id('audio', host_project_id, reference, track.audio_track_id)}",
            "properties": _clean_properties(track.properties),
        }
    )


def _copy_motion(motion: MotionSpec | None) -> MotionSpec | None:
    if motion is None:
        return None
    return motion.validated_copy(update={"properties": _clean_properties(motion.properties)})


def _materialized_scene(
    host_project_id: str,
    reference: SharedVoicedSceneRef,
    source: Scene,
    *,
    order: int,
) -> Scene:
    for transition in (source.transition_in, source.transition_out):
        if transition is not None and (
            transition.transition_type != "cut" or transition.duration_seconds != 0.0
        ):
            raise LongFormSharedSceneConflictError(
                "shared voiced scenes with neighbor-dependent transitions are not supported"
            )
    properties = _clean_properties(source.properties)
    properties.update(
        {
            "pr24_owner": _SHARED_OWNER,
            "pr24_source_project_id": reference.source_project_id,
            "pr24_source_scene_id": reference.source_scene_id,
            "pr24_source_scene_sha256": reference.source_scene_sha256,
            "pr24_pr22_scene_sha256": reference.pr22_scene_sha256,
            "pr24_pr23_scene_plan_sha256": reference.pr23_scene_plan_sha256,
        }
    )
    return source.validated_copy(
        update={
            "scene_id": f"cf_scene_{_owned_id('scene', host_project_id, reference, source.scene_id)}",
            "order": order,
            "transition_in": None,
            "transition_out": None,
            "motion": _copy_motion(source.motion),
            "overlays": tuple(
                _copy_overlay(host_project_id, reference, item) for item in source.overlays
            ),
            "audio_tracks": tuple(
                _copy_track(host_project_id, reference, item) for item in source.audio_tracks
            ),
            "properties": properties,
        }
    )


def _scene_asset_refs(scene: Scene) -> tuple[AssetRef, ...]:
    refs: list[AssetRef] = []
    if scene.media is not None:
        refs.append(scene.media)
    refs.extend(item.asset_ref for item in scene.overlays if item.asset_ref is not None)
    refs.extend(item.asset_ref for item in scene.audio_tracks if item.asset_ref is not None)
    return tuple(refs)


class LongFormSharedSceneWorkflow:
    """Own only PR24 host copies while revalidating every source against PR22/PR23."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise LongFormSharedSceneConflictError(f"unknown long-form host project: {project_id}")
        raw = str(row["manifest_json"])
        return load_json(Project, raw), raw

    def _cas(self, expected_json: str, project: Project) -> Project:
        updated = VoicedSceneWorkflow._invalidate_pr10_render_identity(project)
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
                raise LongFormSharedSceneConflictError(
                    f"long-form host changed concurrently: {updated.project_id}"
                )
        return updated

    @staticmethod
    def _validate_owned_state(
        project: Project,
        manifest: ProjectLongFormSharedManifest,
    ) -> None:
        current = {scene.scene_id: scene for scene in project.scenes if _owned_scene(scene)}
        expected_ids = {item.materialized_scene_id for item in manifest.bindings}
        if set(current) != expected_ids:
            raise LongFormSharedSceneConflictError(
                "PR24 shared-scene ownership no longer matches materialized scene IDs"
            )
        for binding in manifest.bindings:
            scene = current[binding.materialized_scene_id]
            if _digest(scene) != binding.materialized_scene_sha256:
                raise LongFormSharedSceneConflictError(
                    "PR24 shared-scene materialization drifted after capture"
                )

    @staticmethod
    def _reject_orphans(project: Project) -> None:
        if any(_owned_scene(scene) for scene in project.scenes):
            raise LongFormSharedSceneConflictError(
                "orphaned PR24 shared-scene materialization without manifest"
            )

    def validate_snapshot(self, project: Project) -> ProjectLongFormSharedManifest | None:
        manifest = long_form_shared_manifest(project)
        if manifest is None:
            self._reject_orphans(project)
            return None
        self._validate_owned_state(project, manifest)
        local_scenes = tuple(scene for scene in project.scenes if not _owned_scene(scene))
        base_order = len(local_scenes)
        for index, binding in enumerate(manifest.bindings):
            source_project, source_manifest = _source_snapshot(
                self.library,
                binding.reference.source_project_id,
            )
            source_scene, source_plan = _select_scene_authority(
                source_project,
                source_manifest,
                binding.reference.source_scene_id,
            )
            if _digest(source_scene) != binding.reference.source_scene_sha256:
                raise LongFormSharedSceneConflictError(
                    "shared source Scene changed after host materialization"
                )
            if source_plan.pr22_scene_sha256 != binding.reference.pr22_scene_sha256:
                raise LongFormSharedSceneConflictError(
                    "shared source PR22 authority changed after host materialization"
                )
            if _digest(source_plan) != binding.reference.pr23_scene_plan_sha256:
                raise LongFormSharedSceneConflictError(
                    "shared source PR23 authority changed after host materialization"
                )
            expected = _materialized_scene(
                project.project_id,
                binding.reference,
                source_scene,
                order=base_order + index,
            )
            if expected.scene_id != binding.materialized_scene_id or _digest(expected) != binding.materialized_scene_sha256:
                raise LongFormSharedSceneConflictError(
                    "PR24 host copy no longer matches exact shared source authority"
                )
        return manifest

    @staticmethod
    def _merge_provenance(
        host: Project,
        source_projects: tuple[Project, ...],
        source_scenes: tuple[Scene, ...],
    ) -> tuple[tuple[AssetRef, ...], tuple[SourceRecord, ...]]:
        refs = list(host.source_refs)
        records = {item.source_id: item for item in host.source_records}
        ref_keys = {(item.asset_id, item.source_id, item.role) for item in refs}
        for source_project, source_scene in zip(source_projects, source_scenes):
            source_records = {item.source_id: item for item in source_project.source_records}
            for asset_ref in _scene_asset_refs(source_scene):
                if asset_ref.source_id is None:
                    continue
                record = source_records.get(asset_ref.source_id)
                if record is None or record.asset_id != asset_ref.asset_id:
                    raise LongFormSharedSceneConflictError(
                        "shared source Scene provenance is missing or inconsistent"
                    )
                existing = records.get(record.source_id)
                if existing is not None and existing != record:
                    raise LongFormSharedSceneConflictError(
                        "shared source provenance collides with host source identity"
                    )
                records[record.source_id] = record
                copied_ref = AssetRef(
                    asset_id=record.asset_id,
                    source_id=record.source_id,
                    role="pr24_shared_source",
                )
                key = (copied_ref.asset_id, copied_ref.source_id, copied_ref.role)
                if key not in ref_keys:
                    refs.append(copied_ref)
                    ref_keys.add(key)
        return tuple(refs), tuple(records.values())

    def materialize(
        self,
        project_id: str,
        references: tuple[SharedVoicedSceneRef, ...],
    ) -> ProjectLongFormSharedManifest:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_STATES:
            raise LongFormSharedSceneConflictError(
                f"shared scenes cannot mutate host in state {project.state.value}"
            )
        previous = long_form_shared_manifest(project)
        if previous is None:
            self._reject_orphans(project)
        else:
            self._validate_owned_state(project, previous)

        source_keys = tuple(
            (item.source_project_id, item.source_scene_id) for item in references
        )
        if len(set(source_keys)) != len(source_keys):
            raise LongFormSharedSceneConflictError("shared source scenes may not be duplicated")
        if any(item.source_project_id == project.project_id for item in references):
            raise LongFormSharedSceneConflictError(
                "PR24 shared scene references must cross a Project boundary"
            )

        local_scenes = tuple(scene for scene in project.scenes if not _owned_scene(scene))
        source_projects: list[Project] = []
        source_scenes: list[Scene] = []
        materialized: list[Scene] = []
        bindings: list[LongFormSharedSceneBinding] = []
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
            if _owned_scene(source_scene):
                raise LongFormSharedSceneConflictError(
                    "transitive PR24 shared-scene references are not supported"
                )
            copied = _materialized_scene(
                project.project_id,
                reference,
                source_scene,
                order=len(local_scenes) + index,
            )
            materialized.append(copied)
            bindings.append(
                LongFormSharedSceneBinding(
                    reference=reference,
                    materialized_scene_id=copied.scene_id,
                    materialized_scene_sha256=_digest(copied),
                )
            )
            source_projects.append(source_project)
            source_scenes.append(source_scene)

        all_scene_ids = {scene.scene_id for scene in local_scenes}
        all_overlay_ids = {
            overlay.overlay_id for scene in local_scenes for overlay in scene.overlays
        }
        all_audio_ids = {
            track.audio_track_id for scene in local_scenes for track in scene.audio_tracks
        }
        for scene in materialized:
            if scene.scene_id in all_scene_ids:
                raise LongFormSharedSceneConflictError("PR24 deterministic Scene ID collision")
            overlay_ids = {item.overlay_id for item in scene.overlays}
            audio_ids = {item.audio_track_id for item in scene.audio_tracks}
            if all_overlay_ids.intersection(overlay_ids):
                raise LongFormSharedSceneConflictError("PR24 deterministic Overlay ID collision")
            if all_audio_ids.intersection(audio_ids):
                raise LongFormSharedSceneConflictError("PR24 deterministic AudioTrack ID collision")
            all_scene_ids.add(scene.scene_id)
            all_overlay_ids.update(overlay_ids)
            all_audio_ids.update(audio_ids)

        source_refs, source_records = self._merge_provenance(
            project,
            tuple(source_projects),
            tuple(source_scenes),
        )
        manifest = ProjectLongFormSharedManifest(
            project_id=project.project_id,
            bindings=tuple(bindings),
        )
        metadata = dict(project.metadata)
        metadata[_SHARED_METADATA_KEY] = manifest.model_dump(mode="json")
        updated = project.validated_copy(
            update={
                "scenes": (*local_scenes, *materialized),
                "source_refs": source_refs,
                "source_records": source_records,
                "metadata": metadata,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._cas(expected_json, updated)
        return manifest

    def dematerialize(self, project_id: str) -> bool:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_STATES:
            raise LongFormSharedSceneConflictError(
                f"shared scenes cannot mutate host in state {project.state.value}"
            )
        manifest = long_form_shared_manifest(project)
        if manifest is None:
            self._reject_orphans(project)
            return False
        self._validate_owned_state(project, manifest)
        scenes = tuple(scene for scene in project.scenes if not _owned_scene(scene))
        metadata = dict(project.metadata)
        metadata.pop(_SHARED_METADATA_KEY, None)
        updated = project.validated_copy(
            update={
                "scenes": scenes,
                "metadata": metadata,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._cas(expected_json, updated)
        return True


__all__ = [
    "LongFormSharedSceneBinding",
    "LongFormSharedSceneWorkflow",
    "ProjectLongFormSharedManifest",
    "long_form_shared_manifest",
]
