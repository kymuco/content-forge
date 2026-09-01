"""PR23 reversible voiced-scene mix and camera presentation over current PR22 state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from content_forge.application.dialogue import ProjectDialogueManifest, SceneFocusHint
from content_forge.application.dialogue_pr19_integrity import validated_dialogue_manifest
from content_forge.application.voiced_story import (
    ProjectVoicedStoryManifest,
    VoicedStoryConflictError,
    VoicedStoryError,
    VoicedStoryWorkflow,
    _scene_materialization_matches,
    voiced_story_manifest,
)
from content_forge.core import (
    AudioTrack,
    FitMode,
    MediaType,
    MotionSpec,
    NormalizedPoint,
    NormalizedRect,
    Project,
    ProjectState,
    Scene,
    dump_json,
    load_json,
)
from content_forge.core.ids import EntityKind, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.storage import LocalLibrary

_PRESENTATION_MANIFEST_VERSION = "pr23_voiced_scene_manifest_v1"
_PRESENTATION_PLAN_VERSION = "pr23_voiced_scene_plan_v1"
_PRESET_VERSION = "pr23_voiced_scene_preset_v1"
_MIX_POLICY_VERSION = "pr23_voiced_scene_mix_policy_v1"
_CAMERA_POLICY_VERSION = "pr23_voiced_scene_camera_policy_v1"
_SCENE_PLAN_VERSION = "pr23_voiced_scene_scene_plan_v1"
_TRACK_PLAN_VERSION = "pr23_voiced_scene_track_plan_v1"
_QC_VERSION = "pr23_voiced_scene_qc_v1"
_OWNED_TRACK_VERSION = "pr23_owned_track_v1"
_OWNED_MOTION_VERSION = "pr23_owned_motion_v1"
_PRESENTATION_METADATA_KEY = "pr23_voiced_scene"
_MIX_OWNER = "pr23_voiced_mix_v1"
_CAMERA_OWNER = "pr23_camera_v1"
_EDITABLE_STATES = frozenset({ProjectState.DRAFT, ProjectState.PREPARED, ProjectState.READY})
_DUCKABLE_TYPES = frozenset({"music", "ambience"})
_MAX_QC_ISSUES = 10000
_MAX_TRACKS = 10000
_MAX_SCENES = 10000


class VoicedSceneError(RuntimeError):
    pass


class VoicedSceneConflictError(VoicedSceneError):
    pass


class VoicedSceneNotFoundError(VoicedSceneError):
    pass


class VoicedSceneNotReadyError(VoicedSceneError):
    pass


class VoicedSceneValidationError(VoicedSceneError):
    pass


class VoicedSceneMixPolicy(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_mix_policy_v1"] = _MIX_POLICY_VERSION
    music_duck_db: float = Field(default=-10.0, ge=-60.0, le=0.0)
    ambience_duck_db: float = Field(default=-6.0, ge=-60.0, le=0.0)
    minimum_pause_seconds: float = Field(default=0.08, ge=0.0, le=10.0)
    maximum_pause_seconds: float = Field(default=2.0, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def validate_pause_bounds(self):
        if self.maximum_pause_seconds < self.minimum_pause_seconds:
            raise ValueError("maximum voiced-scene pause must not be below minimum")
        return self


class VoicedSceneCameraPolicy(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_camera_policy_v1"] = _CAMERA_POLICY_VERSION
    face_start_scale: float = Field(default=0.86, gt=0.0, le=1.0)
    face_end_scale: float = Field(default=0.74, gt=0.0, le=1.0)
    crop_start_scale: float = Field(default=1.0, gt=0.0, le=1.0)
    crop_end_scale: float = Field(default=0.90, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_zoom_direction(self):
        if self.face_end_scale > self.face_start_scale:
            raise ValueError("face camera policy must zoom inward or hold")
        if self.crop_end_scale > self.crop_start_scale:
            raise ValueError("crop camera policy must zoom inward or hold")
        return self


class VoicedScenePreset(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_preset_v1"] = _PRESET_VERSION
    preset_id: RegistryKey = "natural_dialogue"
    version: str = Field(default="1", min_length=1, max_length=64)
    mix: VoicedSceneMixPolicy = Field(default_factory=VoicedSceneMixPolicy)
    camera: VoicedSceneCameraPolicy = Field(default_factory=VoicedSceneCameraPolicy)


def natural_dialogue_preset() -> VoicedScenePreset:
    return VoicedScenePreset()


class VoicedSceneQCIssue(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_qc_v1"] = _QC_VERSION
    code: RegistryKey
    severity: Literal["warning", "blocking"]
    scene_id: str | None = None
    line_id: str | None = Field(default=None, pattern=r"^dlg_ocr_[0-9]{4}$")

    @model_validator(mode="after")
    def validate_ids(self):
        if self.scene_id is not None:
            require_entity_id(self.scene_id, EntityKind.SCENE)
        return self


class VoicedSceneScenePlan(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_scene_plan_v1"] = _SCENE_PLAN_VERSION
    scene_id: str
    pr22_scene_sha256: SHA256
    camera_action: Literal["retain", "focus_zoom"]
    camera_source: Literal["none", "face_hint", "explicit_crop", "speaker_unresolved"]
    proposed_motion: MotionSpec | None = None
    issues: tuple[VoicedSceneQCIssue, ...] = Field(default=(), max_length=_MAX_QC_ISSUES)

    @model_validator(mode="after")
    def validate_scene(self):
        require_entity_id(self.scene_id, EntityKind.SCENE)
        if self.camera_action == "focus_zoom" and self.proposed_motion is None:
            raise ValueError("focus_zoom scene plan requires proposed motion")
        if self.camera_action == "retain" and self.proposed_motion is not None:
            raise ValueError("retain scene plan must not propose replacement motion")
        return self


class VoicedSceneTrackPlan(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_track_plan_v1"] = _TRACK_PLAN_VERSION
    audio_track_id: str
    scope_scene_id: str | None = None
    track_type: Literal["music", "ambience"]
    duck_db: float = Field(ge=-60.0, le=0.0)

    @model_validator(mode="after")
    def validate_ids(self):
        require_entity_id(self.audio_track_id, EntityKind.AUDIO)
        if self.scope_scene_id is not None:
            require_entity_id(self.scope_scene_id, EntityKind.SCENE)
        return self


class ProjectVoicedScenePlan(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_plan_v1"] = _PRESENTATION_PLAN_VERSION
    project_id: str
    pr22_manifest_sha256: SHA256
    preset: VoicedScenePreset = Field(default_factory=natural_dialogue_preset)
    scenes: tuple[VoicedSceneScenePlan, ...] = Field(default=(), max_length=_MAX_SCENES)
    tracks: tuple[VoicedSceneTrackPlan, ...] = Field(default=(), max_length=_MAX_TRACKS)
    issues: tuple[VoicedSceneQCIssue, ...] = Field(default=(), max_length=_MAX_QC_ISSUES)

    @model_validator(mode="after")
    def validate_plan(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        scene_ids = tuple(item.scene_id for item in self.scenes)
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("voiced-scene plan scene IDs must be unique")
        track_keys = tuple((item.scope_scene_id, item.audio_track_id) for item in self.tracks)
        if len(set(track_keys)) != len(track_keys):
            raise ValueError("voiced-scene track plan entries must be unique")
        return self

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "blocking" for issue in self.issues)


class VoicedSceneOwnedTrack(FrozenModel):
    contract_version: Literal["pr23_owned_track_v1"] = _OWNED_TRACK_VERSION
    scope_scene_id: str | None = None
    base_track: AudioTrack
    materialized_track: AudioTrack

    @model_validator(mode="after")
    def validate_track(self):
        if self.scope_scene_id is not None:
            require_entity_id(self.scope_scene_id, EntityKind.SCENE)
        if self.base_track.audio_track_id != self.materialized_track.audio_track_id:
            raise ValueError("PR23 owned track identity must be stable")
        return self


class VoicedSceneOwnedMotion(FrozenModel):
    contract_version: Literal["pr23_owned_motion_v1"] = _OWNED_MOTION_VERSION
    scene_id: str
    base_motion: MotionSpec | None = None
    materialized_motion: MotionSpec

    @model_validator(mode="after")
    def validate_motion(self):
        require_entity_id(self.scene_id, EntityKind.SCENE)
        return self


class ProjectVoicedSceneManifest(FrozenModel):
    contract_version: Literal["pr23_voiced_scene_manifest_v1"] = _PRESENTATION_MANIFEST_VERSION
    project_id: str
    plan: ProjectVoicedScenePlan
    owned_tracks: tuple[VoicedSceneOwnedTrack, ...] = Field(default=(), max_length=_MAX_TRACKS)
    owned_motions: tuple[VoicedSceneOwnedMotion, ...] = Field(default=(), max_length=_MAX_SCENES)

    @model_validator(mode="after")
    def validate_manifest(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        if self.plan.project_id != self.project_id:
            raise ValueError("PR23 plan project identity mismatch")
        track_keys = tuple(
            (item.scope_scene_id, item.base_track.audio_track_id) for item in self.owned_tracks
        )
        if len(set(track_keys)) != len(track_keys):
            raise ValueError("PR23 owned track identities must be unique")
        motion_ids = tuple(item.scene_id for item in self.owned_motions)
        if len(set(motion_ids)) != len(motion_ids):
            raise ValueError("PR23 owned motion scene IDs must be unique")
        return self


def _metadata(project: Project) -> dict[str, object]:
    metadata = project.model_dump(mode="json")["metadata"]
    if not isinstance(metadata, dict):  # pragma: no cover - core Project contract
        raise VoicedSceneValidationError("project metadata is malformed")
    return metadata


def voiced_scene_manifest(project: Project) -> ProjectVoicedSceneManifest | None:
    raw = _metadata(project).get(_PRESENTATION_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise VoicedSceneValidationError("stored PR23 voiced-scene metadata is malformed")
    try:
        manifest = ProjectVoicedSceneManifest.model_validate(raw)
    except Exception as exc:
        raise VoicedSceneValidationError("stored PR23 voiced-scene manifest is malformed") from exc
    if manifest.project_id != project.project_id:
        raise VoicedSceneConflictError("PR23 voiced-scene manifest project identity mismatch")
    return manifest


def _digest(value: FrozenModel) -> str:
    encoded = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def voiced_story_digest(manifest: ProjectVoicedStoryManifest) -> str:
    return _digest(manifest)


def _issue(
    code: str,
    severity: Literal["warning", "blocking"],
    *,
    scene_id: str | None = None,
    line_id: str | None = None,
) -> VoicedSceneQCIssue:
    return VoicedSceneQCIssue(
        code=code,
        severity=severity,
        scene_id=scene_id,
        line_id=line_id,
    )


def _focus_center(crop: NormalizedRect) -> NormalizedPoint:
    return NormalizedPoint(
        x=crop.x + crop.width / 2.0,
        y=crop.y + crop.height / 2.0,
    )


def _focus_motion(
    hint: SceneFocusHint,
    policy: VoicedSceneCameraPolicy,
    *,
    preset: VoicedScenePreset,
) -> MotionSpec | None:
    if hint.mode == "face":
        assert hint.face is not None
        return MotionSpec(
            motion_type="focus_zoom",
            focus=hint.face,
            properties={
                "pr23_owner": _CAMERA_OWNER,
                "preset_id": preset.preset_id,
                "preset_version": preset.version,
                "focus_source": "face_hint",
                "start_scale": policy.face_start_scale,
                "end_scale": policy.face_end_scale,
            },
        )
    if hint.mode == "explicit_crop":
        assert hint.crop is not None
        return MotionSpec(
            motion_type="focus_zoom",
            focus=_focus_center(hint.crop),
            properties={
                "pr23_owner": _CAMERA_OWNER,
                "preset_id": preset.preset_id,
                "preset_version": preset.version,
                "focus_source": "explicit_crop",
                "focus_crop": hint.crop.model_dump(mode="json"),
                "start_scale": policy.crop_start_scale,
                "end_scale": policy.crop_end_scale,
            },
        )
    return None


def _track_with_duck(track: AudioTrack, *, duck_db: float, preset: VoicedScenePreset) -> AudioTrack:
    properties = track.model_dump(mode="json")["properties"]
    properties["pr23_owner"] = _MIX_OWNER
    properties["pr23_preset_id"] = preset.preset_id
    properties["pr23_preset_version"] = preset.version
    properties["duck_db"] = duck_db
    return track.validated_copy(update={"properties": properties})


def _is_pr23_track(track: AudioTrack) -> bool:
    return track.properties.get("pr23_owner") == _MIX_OWNER


def _is_pr23_motion(motion: MotionSpec | None) -> bool:
    return motion is not None and motion.properties.get("pr23_owner") == _CAMERA_OWNER


class VoicedSceneWorkflow:
    """Derive and reversibly materialize PR23 presentation over exact PR22 authority."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.voiced_story = VoicedStoryWorkflow(library)

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise VoicedSceneNotFoundError(f"unknown project: {project_id}")
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
                raise VoicedSceneConflictError(
                    f"project changed concurrently: {updated.project_id}"
                )
        return updated

    def _validated_pr22(self, project: Project) -> ProjectVoicedStoryManifest:
        stored = voiced_story_manifest(project)
        if stored is None:
            raise VoicedSceneNotReadyError("PR23 requires materialized PR22 voiced-story state")
        try:
            expected = self.voiced_story.derive(project, policy=stored.timing_policy)
        except VoicedStoryError as exc:
            raise VoicedSceneConflictError(f"current PR22 authority is invalid: {exc}") from exc
        if stored != expected or not _scene_materialization_matches(project, stored):
            raise VoicedSceneConflictError(
                "materialized PR22 voiced story no longer matches current upstream authority"
            )
        return stored

    @staticmethod
    def _current_track(project: Project, scope_scene_id: str | None, track_id: str) -> AudioTrack | None:
        tracks = project.audio_tracks
        if scope_scene_id is not None:
            scene = next((item for item in project.scenes if item.scene_id == scope_scene_id), None)
            if scene is None:
                return None
            tracks = scene.audio_tracks
        return next((item for item in tracks if item.audio_track_id == track_id), None)

    @staticmethod
    def _current_motion(project: Project, scene_id: str) -> MotionSpec | None:
        scene = next((item for item in project.scenes if item.scene_id == scene_id), None)
        if scene is None:
            raise VoicedSceneConflictError("PR23 owned motion references missing scene")
        return scene.motion

    def _validate_owned_state(self, project: Project, manifest: ProjectVoicedSceneManifest) -> None:
        for owned in manifest.owned_tracks:
            current = self._current_track(
                project,
                owned.scope_scene_id,
                owned.materialized_track.audio_track_id,
            )
            if current != owned.materialized_track:
                raise VoicedSceneConflictError("PR23 owned audio presentation state drifted")
        for owned in manifest.owned_motions:
            if self._current_motion(project, owned.scene_id) != owned.materialized_motion:
                raise VoicedSceneConflictError("PR23 owned camera presentation state drifted")

    def _reject_orphans(self, project: Project) -> None:
        if any(_is_pr23_track(track) for track in project.audio_tracks):
            raise VoicedSceneConflictError("orphaned PR23 global audio presentation state")
        for scene in project.scenes:
            if _is_pr23_motion(scene.motion):
                raise VoicedSceneConflictError("orphaned PR23 camera presentation state")
            if any(_is_pr23_track(track) for track in scene.audio_tracks):
                raise VoicedSceneConflictError("orphaned PR23 scene audio presentation state")

    def _base_project(
        self,
        project: Project,
        manifest: ProjectVoicedSceneManifest | None,
    ) -> Project:
        if manifest is None:
            self._reject_orphans(project)
            return project
        self._validate_owned_state(project, manifest)
        track_base = {
            (owned.scope_scene_id, owned.base_track.audio_track_id): owned.base_track
            for owned in manifest.owned_tracks
        }
        motion_base = {owned.scene_id: owned.base_motion for owned in manifest.owned_motions}

        global_tracks = tuple(
            track_base.get((None, track.audio_track_id), track)
            for track in project.audio_tracks
        )
        scenes: list[Scene] = []
        for scene in project.scenes:
            scene_tracks = tuple(
                track_base.get((scene.scene_id, track.audio_track_id), track)
                for track in scene.audio_tracks
            )
            scenes.append(
                scene.validated_copy(
                    update={
                        "audio_tracks": scene_tracks,
                        "motion": motion_base.get(scene.scene_id, scene.motion),
                    }
                )
            )
        metadata = _metadata(project)
        metadata.pop(_PRESENTATION_METADATA_KEY, None)
        return project.validated_copy(
            update={
                "audio_tracks": global_tracks,
                "scenes": tuple(scenes),
                "metadata": metadata,
            }
        )

    def _scene_plan(
        self,
        project: Project,
        dialogue: ProjectDialogueManifest,
        pr22: ProjectVoicedStoryManifest,
        preset: VoicedScenePreset,
    ) -> tuple[VoicedSceneScenePlan, ...]:
        dialogue_by_scene = {item.scene_id: item for item in dialogue.scenes}
        core_by_scene = {item.scene_id: item for item in project.scenes}
        plans: list[VoicedSceneScenePlan] = []
        for voiced in pr22.scenes:
            scene = core_by_scene.get(voiced.scene_id)
            dialogue_scene = dialogue_by_scene.get(voiced.scene_id)
            if scene is None or dialogue_scene is None:
                raise VoicedSceneConflictError("PR23 scene no longer exists in PR19/core authority")
            issues: list[VoicedSceneQCIssue] = []
            for left, right in zip(voiced.lines, voiced.lines[1:]):
                gap = right.start_seconds - left.end_seconds
                if gap < -1e-6:
                    issues.append(
                        _issue(
                            "dialogue_overlap",
                            "blocking",
                            scene_id=voiced.scene_id,
                            line_id=right.line_id,
                        )
                    )
                elif gap + 1e-6 < preset.mix.minimum_pause_seconds:
                    issues.append(
                        _issue(
                            "pause_below_preset_minimum",
                            "warning",
                            scene_id=voiced.scene_id,
                            line_id=right.line_id,
                        )
                    )
                elif gap - 1e-6 > preset.mix.maximum_pause_seconds:
                    issues.append(
                        _issue(
                            "pause_above_preset_maximum",
                            "warning",
                            scene_id=voiced.scene_id,
                            line_id=right.line_id,
                        )
                    )

            hint = dialogue_scene.focus_hint
            motion: MotionSpec | None = None
            source: Literal["none", "face_hint", "explicit_crop", "speaker_unresolved"] = "none"
            action: Literal["retain", "focus_zoom"] = "retain"
            if hint is not None and hint.mode == "speaker":
                source = "speaker_unresolved"
                issues.append(
                    _issue(
                        "speaker_focus_geometry_missing",
                        "warning",
                        scene_id=voiced.scene_id,
                    )
                )
            elif hint is not None:
                if scene.media is None:
                    issues.append(
                        _issue("camera_source_missing", "blocking", scene_id=voiced.scene_id)
                    )
                else:
                    asset = self.library.database.get_asset(scene.media.asset_id)
                    if (
                        asset is None
                        or asset.media_type is not MediaType.IMAGE
                        or asset.width is None
                        or asset.height is None
                    ):
                        issues.append(
                            _issue(
                                "camera_source_geometry_missing",
                                "warning",
                                scene_id=voiced.scene_id,
                            )
                        )
                    elif scene.fit_mode is not FitMode.COVER or scene.crop is not None:
                        issues.append(
                            _issue(
                                "camera_source_fit_unsupported",
                                "warning",
                                scene_id=voiced.scene_id,
                            )
                        )
                    else:
                        motion = _focus_motion(hint, preset.camera, preset=preset)
                        if motion is not None:
                            action = "focus_zoom"
                            source = "face_hint" if hint.mode == "face" else "explicit_crop"
            plans.append(
                VoicedSceneScenePlan(
                    scene_id=voiced.scene_id,
                    pr22_scene_sha256=_digest(voiced),
                    camera_action=action,
                    camera_source=source,
                    proposed_motion=motion,
                    issues=tuple(issues),
                )
            )
        return tuple(plans)

    @staticmethod
    def _track_plans(project: Project, preset: VoicedScenePreset, voiced_scene_ids: set[str]) -> tuple[VoicedSceneTrackPlan, ...]:
        plans: list[VoicedSceneTrackPlan] = []
        for track in project.audio_tracks:
            if track.track_type in _DUCKABLE_TYPES:
                plans.append(
                    VoicedSceneTrackPlan(
                        audio_track_id=track.audio_track_id,
                        track_type=track.track_type,
                        duck_db=(
                            preset.mix.music_duck_db
                            if track.track_type == "music"
                            else preset.mix.ambience_duck_db
                        ),
                    )
                )
        for scene in project.scenes:
            if scene.scene_id not in voiced_scene_ids:
                continue
            for track in scene.audio_tracks:
                if track.track_type in _DUCKABLE_TYPES:
                    plans.append(
                        VoicedSceneTrackPlan(
                            audio_track_id=track.audio_track_id,
                            scope_scene_id=scene.scene_id,
                            track_type=track.track_type,
                            duck_db=(
                                preset.mix.music_duck_db
                                if track.track_type == "music"
                                else preset.mix.ambience_duck_db
                            ),
                        )
                    )
        return tuple(plans)

    def derive(
        self,
        project: Project,
        *,
        preset: VoicedScenePreset | None = None,
    ) -> ProjectVoicedScenePlan:
        selected = preset or natural_dialogue_preset()
        pr22 = self._validated_pr22(project)
        dialogue = validated_dialogue_manifest(project)
        scenes = self._scene_plan(project, dialogue, pr22, selected)
        issues = tuple(issue for scene in scenes for issue in scene.issues)
        tracks = self._track_plans(
            project,
            selected,
            {scene.scene_id for scene in pr22.scenes},
        )
        return ProjectVoicedScenePlan(
            project_id=project.project_id,
            pr22_manifest_sha256=voiced_story_digest(pr22),
            preset=selected,
            scenes=scenes,
            tracks=tracks,
            issues=issues,
        )

    def preview(
        self,
        project_id: str,
        *,
        preset: VoicedScenePreset | None = None,
    ) -> ProjectVoicedScenePlan:
        project, _ = self._snapshot(project_id)
        stored = voiced_scene_manifest(project)
        base = self._base_project(project, stored)
        selected = preset if preset is not None else (None if stored is None else stored.plan.preset)
        return self.derive(base, preset=selected)

    def _apply_plan(
        self,
        base: Project,
        plan: ProjectVoicedScenePlan,
    ) -> tuple[Project, tuple[VoicedSceneOwnedTrack, ...], tuple[VoicedSceneOwnedMotion, ...]]:
        track_plan = {(item.scope_scene_id, item.audio_track_id): item for item in plan.tracks}
        owned_tracks: list[VoicedSceneOwnedTrack] = []
        owned_motions: list[VoicedSceneOwnedMotion] = []

        global_tracks: list[AudioTrack] = []
        for track in base.audio_tracks:
            target = track_plan.get((None, track.audio_track_id))
            if target is None:
                global_tracks.append(track)
                continue
            materialized = _track_with_duck(track, duck_db=target.duck_db, preset=plan.preset)
            owned_tracks.append(
                VoicedSceneOwnedTrack(
                    base_track=track,
                    materialized_track=materialized,
                )
            )
            global_tracks.append(materialized)

        camera_plan = {item.scene_id: item for item in plan.scenes}
        scenes: list[Scene] = []
        for scene in base.scenes:
            scene_tracks: list[AudioTrack] = []
            for track in scene.audio_tracks:
                target = track_plan.get((scene.scene_id, track.audio_track_id))
                if target is None:
                    scene_tracks.append(track)
                    continue
                materialized = _track_with_duck(
                    track,
                    duck_db=target.duck_db,
                    preset=plan.preset,
                )
                owned_tracks.append(
                    VoicedSceneOwnedTrack(
                        scope_scene_id=scene.scene_id,
                        base_track=track,
                        materialized_track=materialized,
                    )
                )
                scene_tracks.append(materialized)

            motion = scene.motion
            planned_scene = camera_plan.get(scene.scene_id)
            if (
                planned_scene is not None
                and planned_scene.camera_action == "focus_zoom"
                and planned_scene.proposed_motion is not None
            ):
                materialized_motion = planned_scene.proposed_motion
                owned_motions.append(
                    VoicedSceneOwnedMotion(
                        scene_id=scene.scene_id,
                        base_motion=scene.motion,
                        materialized_motion=materialized_motion,
                    )
                )
                motion = materialized_motion
            scenes.append(
                scene.validated_copy(
                    update={
                        "audio_tracks": tuple(scene_tracks),
                        "motion": motion,
                    }
                )
            )
        return (
            base.validated_copy(
                update={
                    "audio_tracks": tuple(global_tracks),
                    "scenes": tuple(scenes),
                }
            ),
            tuple(owned_tracks),
            tuple(owned_motions),
        )

    def materialize(
        self,
        project_id: str,
        *,
        preset: VoicedScenePreset | None = None,
    ) -> ProjectVoicedSceneManifest:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_STATES:
            raise VoicedSceneConflictError(
                f"voiced-scene presentation cannot mutate project in state {project.state.value}"
            )
        previous = voiced_scene_manifest(project)
        base = self._base_project(project, previous)
        selected = preset if preset is not None else (None if previous is None else previous.plan.preset)
        plan = self.derive(base, preset=selected)
        if not plan.passed:
            raise VoicedSceneNotReadyError("PR23 presentation plan has blocking QC issues")
        applied, owned_tracks, owned_motions = self._apply_plan(base, plan)
        manifest = ProjectVoicedSceneManifest(
            project_id=project_id,
            plan=plan,
            owned_tracks=owned_tracks,
            owned_motions=owned_motions,
        )
        metadata = _metadata(applied)
        metadata[_PRESENTATION_METADATA_KEY] = manifest.model_dump(mode="json")
        updated = applied.validated_copy(
            update={
                "metadata": metadata,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        if previous == manifest and updated.model_dump(mode="json", exclude={"updated_at"}) == project.model_dump(mode="json", exclude={"updated_at"}):
            return previous
        self._cas_project(expected_json, updated)
        return manifest

    def manifest(self, project_id: str) -> ProjectVoicedSceneManifest:
        project, _ = self._snapshot(project_id)
        stored = voiced_scene_manifest(project)
        if stored is None:
            raise VoicedSceneNotFoundError("project has no materialized PR23 voiced-scene presentation")
        base = self._base_project(project, stored)
        expected = self.derive(base, preset=stored.plan.preset)
        if expected != stored.plan:
            raise VoicedSceneConflictError("materialized PR23 plan no longer matches current PR22/project authority")
        return stored

    def dematerialize(self, project_id: str) -> bool:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _EDITABLE_STATES:
            raise VoicedSceneConflictError(
                f"voiced-scene presentation cannot mutate project in state {project.state.value}"
            )
        stored = voiced_scene_manifest(project)
        if stored is None:
            self._reject_orphans(project)
            return False
        base = self._base_project(project, stored)
        updated = base.validated_copy(update={"updated_at": datetime.now(timezone.utc)})
        self._cas_project(expected_json, updated)
        return True


__all__ = [
    "ProjectVoicedSceneManifest",
    "ProjectVoicedScenePlan",
    "VoicedSceneCameraPolicy",
    "VoicedSceneConflictError",
    "VoicedSceneError",
    "VoicedSceneMixPolicy",
    "VoicedSceneNotFoundError",
    "VoicedSceneNotReadyError",
    "VoicedScenePreset",
    "VoicedSceneQCIssue",
    "VoicedSceneScenePlan",
    "VoicedSceneTrackPlan",
    "VoicedSceneValidationError",
    "VoicedSceneWorkflow",
    "natural_dialogue_preset",
    "voiced_scene_manifest",
    "voiced_story_digest",
]
