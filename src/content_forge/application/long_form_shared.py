"""PR24 exact-authority references for reusing voiced scenes across projects."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import model_validator

from content_forge.core import EntityKind, Project, Scene, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.storage import LocalLibrary

from .voiced_scene import ProjectVoicedSceneManifest, VoicedSceneScenePlan
from .voiced_scene_hardening import VoicedSceneWorkflow

_SHARED_VOICED_SCENE_REF_VERSION = "pr24_shared_voiced_scene_ref_v1"


class LongFormSharedSceneError(RuntimeError):
    pass


class LongFormSharedSceneNotFoundError(LongFormSharedSceneError):
    pass


class LongFormSharedSceneConflictError(LongFormSharedSceneError):
    pass


class SharedVoicedSceneRef(FrozenModel):
    """Exact reusable scene authority captured from one current PR22/PR23 source scene."""

    contract_version: Literal["pr24_shared_voiced_scene_ref_v1"] = (
        _SHARED_VOICED_SCENE_REF_VERSION
    )
    source_project_id: str
    source_scene_id: str
    source_scene_sha256: SHA256
    pr22_scene_sha256: SHA256
    pr23_scene_plan_sha256: SHA256

    @model_validator(mode="after")
    def validate_ids(self):
        require_entity_id(self.source_project_id, EntityKind.PROJECT)
        require_entity_id(self.source_scene_id, EntityKind.SCENE)
        return self


def _digest(model: FrozenModel) -> str:
    encoded = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_snapshot(
    library: LocalLibrary,
    source_project_id: str,
) -> tuple[Project, ProjectVoicedSceneManifest]:
    require_entity_id(source_project_id, EntityKind.PROJECT)
    project = library.load_project(source_project_id)
    if project is None:
        raise LongFormSharedSceneNotFoundError(
            f"unknown shared-scene source project: {source_project_id}"
        )
    try:
        manifest = VoicedSceneWorkflow(library).validate_snapshot(project)
    except Exception as exc:
        raise LongFormSharedSceneConflictError(
            f"shared-scene source has no current PR22/PR23 authority: {exc}"
        ) from exc
    return project, manifest


def _select_scene_authority(
    project: Project,
    manifest: ProjectVoicedSceneManifest,
    scene_id: str,
) -> tuple[Scene, VoicedSceneScenePlan]:
    require_entity_id(scene_id, EntityKind.SCENE)
    scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
    if scene is None:
        raise LongFormSharedSceneNotFoundError(
            f"shared-scene source scene does not exist: {scene_id}"
        )
    scene_plan = next(
        (item for item in manifest.plan.scenes if item.scene_id == scene_id),
        None,
    )
    if scene_plan is None:
        raise LongFormSharedSceneConflictError(
            "shared scene is not represented by current PR23 voiced presentation authority"
        )
    return scene, scene_plan


def capture_shared_voiced_scene(
    library: LocalLibrary,
    source_project_id: str,
    source_scene_id: str,
) -> SharedVoicedSceneRef:
    """Capture a reusable reference only from an exact currently valid voiced scene."""

    project, manifest = _source_snapshot(library, source_project_id)
    scene, scene_plan = _select_scene_authority(project, manifest, source_scene_id)
    return SharedVoicedSceneRef(
        source_project_id=source_project_id,
        source_scene_id=source_scene_id,
        source_scene_sha256=_digest(scene),
        pr22_scene_sha256=scene_plan.pr22_scene_sha256,
        pr23_scene_plan_sha256=_digest(scene_plan),
    )


def resolve_shared_voiced_scene(
    library: LocalLibrary,
    reference: SharedVoicedSceneRef,
) -> Scene:
    """Resolve a reference from one exact live source snapshot or fail closed."""

    project, manifest = _source_snapshot(library, reference.source_project_id)
    scene, scene_plan = _select_scene_authority(
        project,
        manifest,
        reference.source_scene_id,
    )
    if _digest(scene) != reference.source_scene_sha256:
        raise LongFormSharedSceneConflictError(
            "shared source Scene changed after the PR24 reference was captured"
        )
    if scene_plan.pr22_scene_sha256 != reference.pr22_scene_sha256:
        raise LongFormSharedSceneConflictError(
            "shared source PR22 voiced-scene authority changed after capture"
        )
    if _digest(scene_plan) != reference.pr23_scene_plan_sha256:
        raise LongFormSharedSceneConflictError(
            "shared source PR23 presentation authority changed after capture"
        )
    return scene


__all__ = [
    "LongFormSharedSceneConflictError",
    "LongFormSharedSceneError",
    "LongFormSharedSceneNotFoundError",
    "SharedVoicedSceneRef",
    "capture_shared_voiced_scene",
    "resolve_shared_voiced_scene",
]
