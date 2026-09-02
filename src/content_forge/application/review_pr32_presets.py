"""PR32 preset-aware bootstrap/compile bridge over the existing PR10 authority."""

from __future__ import annotations

from content_forge.core import AttentionMode, MediaType, Project, ProjectState, ReviewPriority, TemplateRef, Variant
from content_forge.profiles.shorts import shorts_final_profile, shorts_preview_profile
from content_forge.templates import HOOK_OVERLAY_TEMPLATE_ID, compile_registered_template

from . import _review_base as _base
from .production_presets import ProductionPresetError, preset_for_project
from .review import ReviewConflictError, ReviewNotReadyError
from .review_pr24_hardening import ReviewService as _FinalReviewService


_BASE_BOOTSTRAP = _base.ReviewService.bootstrap_project
_BASE_FINAL_COMPILE = _FinalReviewService._compile_plan
_BASE_PROJECT_SUMMARY = _FinalReviewService.project_summary


def _preset_bootstrap_project(self: _base.ReviewService, project_id: str) -> Project:
    current = self.get_project(project_id)
    try:
        preset = preset_for_project(current)
    except ProductionPresetError as exc:
        raise ReviewConflictError(f"production preset authority is invalid: {exc}") from exc
    if preset is None:
        return _BASE_BOOTSTRAP(self, project_id)

    def bootstrap(project: Project) -> Project:
        try:
            exact = preset_for_project(project)
        except ProductionPresetError as exc:
            raise ReviewConflictError(f"production preset authority is invalid: {exc}") from exc
        if exact != preset:
            raise ReviewConflictError("production preset changed during bootstrap")
        if bool(project.metadata.get("pr10_review_initialized")):
            return project
        if project.state in _base._TERMINAL_REVIEW_STATES:
            raise ReviewConflictError(
                f"project cannot be bootstrapped from state {project.state.value}"
            )
        if project.template != TemplateRef(
            template_id=preset.template_id,
            version=preset.template_version,
        ):
            raise ReviewConflictError("production preset template changed before bootstrap")
        if not (preset.min_sources <= len(project.scenes) <= preset.max_sources):
            raise ReviewConflictError("production preset scene count is outside its contract")

        for scene in project.scenes:
            if scene.media is None:
                raise ReviewConflictError("production preset scene has no media")
            asset = self.library.database.get_asset(scene.media.asset_id)
            if asset is None or asset.media_type not in {MediaType.IMAGE, MediaType.VIDEO}:
                raise ReviewConflictError("production preset scene references non-visual media")
            if preset.image_only and asset.media_type is not MediaType.IMAGE:
                raise ReviewConflictError("image-only production preset contains video media")
            if asset.media_type is MediaType.VIDEO and (
                asset.duration_seconds is None or asset.duration_seconds <= 0
            ):
                raise ReviewConflictError("production preset video duration is unavailable")

        tasks = list(project.review_tasks)
        existing_types = {task.task_type for task in tasks}
        metadata = dict(project.metadata)
        variants = list(project.variants)
        output_profiles = list(project.output_profiles)
        if not variants:
            variants.append(Variant())
        review_variant = variants[0]
        metadata["review_variant_id"] = review_variant.variant_id

        profile_by_id = {profile.profile_id: profile for profile in output_profiles}
        for expected in (shorts_preview_profile(), shorts_final_profile()):
            stored = profile_by_id.get(expected.profile_id)
            if stored is None:
                output_profiles.append(expected)
            elif stored != expected:
                raise ReviewConflictError(
                    "existing output profile conflicts with PR32 built-in shorts profile"
                )

        def add_task(task) -> None:
            if task.task_type not in existing_types:
                tasks.append(task)
                existing_types.add(task.task_type)

        add_task(
            self._new_task(
                project.project_id,
                _base._AUTO_BOOTSTRAP_TASK,
                attention=AttentionMode.AUTO,
                priority=ReviewPriority.LOW,
                blocking=False,
                payload={
                    "template_id": preset.template_id,
                    "preview_profile_id": _base.SHORTS_PREVIEW_PROFILE_ID,
                },
                resolved=True,
                accepted_value="prepared",
            )
        )
        if preset.requires_hook:
            add_task(
                self._new_task(
                    project.project_id,
                    "hook",
                    priority=ReviewPriority.BLOCKING,
                    payload={"variant_id": review_variant.variant_id, "current": review_variant.hook},
                )
            )
        if preset.review_crop:
            add_task(
                self._new_task(
                    project.project_id,
                    "crop_confirmation",
                    priority=ReviewPriority.HIGH,
                    payload={"scene_ids": [scene.scene_id for scene in project.scenes]},
                )
            )
        # Source ordering is already an explicit human action in the PR32 wizard. Do not
        # ask the user to confirm the same ordering again in PR10.
        add_task(
            self._new_task(
                project.project_id,
                "metadata",
                priority=ReviewPriority.NORMAL,
                blocking=False,
                payload={
                    "variant_id": review_variant.variant_id,
                    "title": review_variant.title,
                    "description": review_variant.description,
                    "hashtags": list(review_variant.hashtags),
                },
            )
        )
        add_task(
            self._new_task(
                project.project_id,
                _base._PREVIEW_TASK,
                priority=ReviewPriority.BLOCKING,
                payload={"status": "not_rendered"},
            )
        )

        metadata["pr10_review_initialized"] = True
        metadata["review_renderable"] = True
        return project.validated_copy(
            update={
                "state": ProjectState.NEEDS_REVIEW,
                "variants": tuple(variants),
                "output_profiles": tuple(output_profiles),
                "review_tasks": tuple(tasks),
                "metadata": metadata,
                "updated_at": _base._utc_now(),
            }
        )

    return self._mutate_project(project_id, bootstrap)


def _preset_compile_plan(self: _FinalReviewService, project: Project, profile_id: str):
    try:
        preset = preset_for_project(project)
    except ProductionPresetError as exc:
        raise ReviewNotReadyError(f"production preset authority is invalid: {exc}") from exc
    if preset is None or preset.template_id == HOOK_OVERLAY_TEMPLATE_ID:
        return _BASE_FINAL_COMPILE(self, project, profile_id)
    if not bool(project.metadata.get("review_renderable")):
        raise ReviewNotReadyError("project is not renderable by the phone production workflow")

    # Preserve the exact render-authority gates installed by PR23/PR24 before bypassing
    # the old hook_overlay-only compiler branch. Earlier PR10/PR17 safety lives around
    # this method in the unchanged review lifecycle rather than inside template compilation.
    guard = getattr(self, "_require_pr23_render_authority", None)
    if callable(guard):
        guard(project)
    guard = getattr(self, "_require_pr24_shared_authority", None)
    if callable(guard):
        guard(project)

    variant = self._review_variant(project)
    try:
        return compile_registered_template(
            project,
            self.library.database,
            profile_id=profile_id,
            variant_id=variant.variant_id,
        )
    except Exception as exc:
        raise ReviewNotReadyError(f"production preset cannot compile for review: {exc}") from exc


def _preset_project_summary(self: _FinalReviewService, project: Project) -> dict[str, object]:
    summary = _BASE_PROJECT_SUMMARY(self, project)
    try:
        preset = preset_for_project(project)
    except ProductionPresetError:
        preset = None
    if preset is not None:
        summary["production_preset_id"] = preset.preset_id
        summary["production_preset_label"] = preset.label
        summary["production_source_count"] = len(project.source_refs)
    return summary


setattr(_preset_bootstrap_project, "_content_forge_pr32_preset_bootstrap", True)
if not getattr(_base.ReviewService.bootstrap_project, "_content_forge_pr32_preset_bootstrap", False):
    setattr(_base.ReviewService, "bootstrap_project", _preset_bootstrap_project)

setattr(_preset_compile_plan, "_content_forge_pr32_preset_compile", True)
if not getattr(_FinalReviewService._compile_plan, "_content_forge_pr32_preset_compile", False):
    setattr(_FinalReviewService, "_compile_plan", _preset_compile_plan)

setattr(_preset_project_summary, "_content_forge_pr32_preset_summary", True)
if not getattr(_FinalReviewService.project_summary, "_content_forge_pr32_preset_summary", False):
    setattr(_FinalReviewService, "project_summary", _preset_project_summary)

ReviewService = _FinalReviewService


__all__ = ["ReviewService"]
