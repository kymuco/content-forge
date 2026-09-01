"""PR24 long-form chapter composition over the canonical Scene/timeline runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, model_validator

from content_forge.core import Asset, EntityKind, Project, RegistryKey, require_entity_id
from content_forge.core.models import FrozenModel, SHA256
from content_forge.timeline import (
    AssetResolver,
    RenderPlan,
    compile_timeline,
    render_plan_digest,
)

_LONG_FORM_CHAPTER_SPEC_VERSION = "pr24_long_form_chapter_spec_v1"
_LONG_FORM_CHAPTER_PLAN_VERSION = "pr24_long_form_chapter_plan_v1"
_LONG_FORM_COMPOSITION_VERSION = "pr24_long_form_composition_v1"
_MAX_CHAPTERS = 1000
_MAX_SCENES_PER_CHAPTER = 10000


class LongFormError(ValueError):
    pass


class LongFormValidationError(LongFormError):
    pass


class LongFormChapterSpec(FrozenModel):
    contract_version: Literal["pr24_long_form_chapter_spec_v1"] = (
        _LONG_FORM_CHAPTER_SPEC_VERSION
    )
    chapter_id: RegistryKey
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    scene_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_SCENES_PER_CHAPTER,
    )

    @model_validator(mode="after")
    def validate_scene_ids(self):
        for scene_id in self.scene_ids:
            require_entity_id(scene_id, EntityKind.SCENE)
        if len(set(self.scene_ids)) != len(self.scene_ids):
            raise ValueError("chapter scene IDs must be unique")
        return self


class LongFormChapterPlan(FrozenModel):
    contract_version: Literal["pr24_long_form_chapter_plan_v1"] = (
        _LONG_FORM_CHAPTER_PLAN_VERSION
    )
    chapter_id: RegistryKey
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    scene_ids: tuple[str, ...] = Field(min_length=1, max_length=_MAX_SCENES_PER_CHAPTER)
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(gt=0.0)
    duration_seconds: float = Field(gt=0.0)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("chapter must have positive duration")
        if abs((self.end_seconds - self.start_seconds) - self.duration_seconds) > 1e-6:
            raise ValueError("chapter duration must equal its start/end interval")
        return self


class LongFormComposition(FrozenModel):
    contract_version: Literal["pr24_long_form_composition_v1"] = (
        _LONG_FORM_COMPOSITION_VERSION
    )
    project_id: str
    profile_id: RegistryKey
    render_plan_digest: SHA256
    total_duration_seconds: float = Field(gt=0.0)
    chapters: tuple[LongFormChapterPlan, ...] = Field(
        min_length=1,
        max_length=_MAX_CHAPTERS,
    )

    @model_validator(mode="after")
    def validate_composition(self):
        require_entity_id(self.project_id, EntityKind.PROJECT)
        chapter_ids = tuple(chapter.chapter_id for chapter in self.chapters)
        if len(set(chapter_ids)) != len(chapter_ids):
            raise ValueError("long-form chapter IDs must be unique")
        if abs(self.chapters[0].start_seconds) > 1e-6:
            raise ValueError("first long-form chapter must start at zero")
        for previous, current in zip(self.chapters, self.chapters[1:]):
            if abs(previous.end_seconds - current.start_seconds) > 1e-6:
                raise ValueError("long-form chapters must be contiguous")
        if abs(self.chapters[-1].end_seconds - self.total_duration_seconds) > 1e-6:
            raise ValueError("last long-form chapter must end at total duration")
        return self


AssetSource = Mapping[str, Asset] | AssetResolver


def _require_long_form_profile(plan: RenderPlan) -> None:
    profile = plan.output_profile
    if (
        profile.properties.get("format_family") != "long_form"
        or profile.properties.get("orientation") != "horizontal"
        or profile.width * 9 != profile.height * 16
    ):
        raise LongFormValidationError(
            "PR24 composition requires a canonical horizontal 16:9 long-form profile"
        )


def _default_chapters(project: Project) -> tuple[LongFormChapterSpec, ...]:
    ordered = tuple(sorted(project.scenes, key=lambda scene: scene.order))
    if not ordered:
        raise LongFormValidationError("long-form composition requires at least one scene")
    return (
        LongFormChapterSpec(
            chapter_id="chapter_1",
            scene_ids=tuple(scene.scene_id for scene in ordered),
        ),
    )


def compile_long_form_composition(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str,
    chapters: tuple[LongFormChapterSpec, ...] | None = None,
) -> tuple[RenderPlan, LongFormComposition]:
    """Compile canonical timeline output and deterministic chapter boundaries together.

    Chapters are deliberately metadata over the canonical Scene order. They may group
    contiguous scenes but may not omit, duplicate, or reorder them.
    """

    render_plan = compile_timeline(project, assets, profile_id=profile_id)
    _require_long_form_profile(render_plan)

    chapter_specs = _default_chapters(project) if chapters is None else chapters
    if not chapter_specs:
        raise LongFormValidationError("long-form composition requires at least one chapter")
    if len(chapter_specs) > _MAX_CHAPTERS:
        raise LongFormValidationError("long-form chapter count exceeds budget")

    canonical_scene_ids = tuple(scene.scene_id for scene in render_plan.scenes)
    flattened = tuple(
        scene_id
        for chapter in chapter_specs
        for scene_id in chapter.scene_ids
    )
    if flattened != canonical_scene_ids:
        raise LongFormValidationError(
            "chapter scene order must exactly cover the canonical timeline scene order"
        )

    planned_by_id = {scene.scene_id: scene for scene in render_plan.scenes}
    chapter_plans: list[LongFormChapterPlan] = []
    for spec in chapter_specs:
        first = planned_by_id[spec.scene_ids[0]]
        last = planned_by_id[spec.scene_ids[-1]]
        chapter_plans.append(
            LongFormChapterPlan(
                chapter_id=spec.chapter_id,
                title=spec.title,
                scene_ids=spec.scene_ids,
                start_seconds=first.start_seconds,
                end_seconds=last.end_seconds,
                duration_seconds=last.end_seconds - first.start_seconds,
            )
        )

    composition = LongFormComposition(
        project_id=project.project_id,
        profile_id=render_plan.output_profile.profile_id,
        render_plan_digest=render_plan_digest(render_plan),
        total_duration_seconds=render_plan.total_duration_seconds,
        chapters=tuple(chapter_plans),
    )
    return render_plan, composition


__all__ = [
    "LongFormChapterPlan",
    "LongFormChapterSpec",
    "LongFormComposition",
    "LongFormError",
    "LongFormValidationError",
    "compile_long_form_composition",
]
