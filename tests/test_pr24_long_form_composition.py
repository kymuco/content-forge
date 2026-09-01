from __future__ import annotations

import pytest

from content_forge.application import (
    LongFormChapterSpec,
    LongFormValidationError,
    compile_long_form_composition,
)
from content_forge.core import Project, Scene
from content_forge.profiles import (
    LONG_FORM_1080P_PROFILE_ID,
    long_form_1080p_profile,
    shorts_final_profile,
)
from content_forge.timeline import render_plan_digest


def _project() -> Project:
    return Project(
        content_kind="long_form_fixture",
        scenes=(
            Scene(order=0, duration_seconds=2.0),
            Scene(order=1, duration_seconds=3.0),
            Scene(order=2, duration_seconds=4.0),
        ),
        output_profiles=(long_form_1080p_profile(),),
    )


def test_pr24_chapters_group_canonical_scene_order_without_retiming() -> None:
    project = _project()
    first, second, third = project.scenes
    plan, composition = compile_long_form_composition(
        project,
        {},
        profile_id=LONG_FORM_1080P_PROFILE_ID,
        chapters=(
            LongFormChapterSpec(
                chapter_id="opening",
                title="Opening",
                scene_ids=(first.scene_id, second.scene_id),
            ),
            LongFormChapterSpec(
                chapter_id="ending",
                title="Ending",
                scene_ids=(third.scene_id,),
            ),
        ),
    )

    assert composition.project_id == project.project_id
    assert composition.profile_id == LONG_FORM_1080P_PROFILE_ID
    assert composition.render_plan_digest == render_plan_digest(plan)
    assert composition.total_duration_seconds == 9.0
    assert composition.chapters[0].scene_ids == (first.scene_id, second.scene_id)
    assert (
        composition.chapters[0].start_seconds,
        composition.chapters[0].end_seconds,
        composition.chapters[0].duration_seconds,
    ) == (0.0, 5.0, 5.0)
    assert (
        composition.chapters[1].start_seconds,
        composition.chapters[1].end_seconds,
        composition.chapters[1].duration_seconds,
    ) == (5.0, 9.0, 4.0)


def test_pr24_default_chapter_exactly_covers_compiled_timeline() -> None:
    project = _project()
    plan, composition = compile_long_form_composition(
        project,
        {},
        profile_id=LONG_FORM_1080P_PROFILE_ID,
    )

    assert len(composition.chapters) == 1
    assert composition.chapters[0].chapter_id == "chapter_1"
    assert composition.chapters[0].scene_ids == tuple(
        scene.scene_id for scene in plan.scenes
    )
    assert composition.chapters[0].start_seconds == 0.0
    assert composition.chapters[0].end_seconds == 9.0


@pytest.mark.parametrize(
    "chapter_scene_indexes",
    [
        ((0,), (2,)),  # omitted scene
        ((0, 1), (1, 2)),  # duplicated scene
        ((1,), (0, 2)),  # reordered scenes
    ],
)
def test_pr24_rejects_chapters_that_do_not_exactly_partition_scene_order(
    chapter_scene_indexes,
) -> None:
    project = _project()
    specs = tuple(
        LongFormChapterSpec(
            chapter_id=f"chapter_{index}",
            scene_ids=tuple(project.scenes[item].scene_id for item in indexes),
        )
        for index, indexes in enumerate(chapter_scene_indexes)
    )

    with pytest.raises(LongFormValidationError, match="exactly cover"):
        compile_long_form_composition(
            project,
            {},
            profile_id=LONG_FORM_1080P_PROFILE_ID,
            chapters=specs,
        )


def test_pr24_rejects_vertical_profile_as_long_form_authority() -> None:
    project = Project(
        content_kind="long_form_fixture",
        scenes=(Scene(order=0, duration_seconds=1.0),),
        output_profiles=(shorts_final_profile(),),
    )

    with pytest.raises(LongFormValidationError, match="horizontal 16:9"):
        compile_long_form_composition(
            project,
            {},
            profile_id="shorts_final",
        )
