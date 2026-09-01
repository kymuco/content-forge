from __future__ import annotations

from content_forge.core import Project, Scene
from content_forge.profiles import (
    LONG_FORM_1080P_PROFILE_ID,
    LONG_FORM_1440P_PROFILE_ID,
    long_form_1080p_profile,
    long_form_1440p_profile,
)
from content_forge.timeline import compile_timeline, render_plan_digest


def test_pr24_long_form_profiles_are_canonical_16_by_9_outputs() -> None:
    profile_1080 = long_form_1080p_profile()
    profile_1440 = long_form_1440p_profile(fps=60.0)

    assert profile_1080.profile_id == LONG_FORM_1080P_PROFILE_ID
    assert (profile_1080.width, profile_1080.height, profile_1080.fps) == (1920, 1080, 30.0)
    assert profile_1080.video_bitrate_kbps == 12000
    assert profile_1080.audio_bitrate_kbps == 192
    assert profile_1080.properties["orientation"] == "horizontal"
    assert profile_1080.properties["format_family"] == "long_form"
    assert profile_1080.properties["aspect_ratio"] == "16:9"

    assert profile_1440.profile_id == LONG_FORM_1440P_PROFILE_ID
    assert (profile_1440.width, profile_1440.height, profile_1440.fps) == (2560, 1440, 60.0)
    assert profile_1440.video_bitrate_kbps == 24000
    assert profile_1440.audio_bitrate_kbps == 192
    assert profile_1440.properties["resolution"] == "1440p"


def test_pr24_same_scene_graph_compiles_to_both_long_form_profiles_with_distinct_cache_identity() -> None:
    first = Scene(order=0, duration_seconds=2.5)
    second = Scene(order=1, duration_seconds=4.0)
    project = Project(
        content_kind="long_form_fixture",
        scenes=(first, second),
        output_profiles=(long_form_1080p_profile(), long_form_1440p_profile()),
    )

    plan_1080 = compile_timeline(
        project,
        {},
        profile_id=LONG_FORM_1080P_PROFILE_ID,
    )
    plan_1440 = compile_timeline(
        project,
        {},
        profile_id=LONG_FORM_1440P_PROFILE_ID,
    )

    assert tuple(scene.scene_id for scene in plan_1080.scenes) == (first.scene_id, second.scene_id)
    assert tuple(scene.scene_id for scene in plan_1440.scenes) == (first.scene_id, second.scene_id)
    assert tuple(scene.start_seconds for scene in plan_1080.scenes) == (0.0, 2.5)
    assert tuple(scene.start_seconds for scene in plan_1440.scenes) == (0.0, 2.5)
    assert plan_1080.total_duration_seconds == 6.5
    assert plan_1440.total_duration_seconds == 6.5
    assert plan_1080.output_profile.profile_id == LONG_FORM_1080P_PROFILE_ID
    assert plan_1440.output_profile.profile_id == LONG_FORM_1440P_PROFILE_ID
    assert render_plan_digest(plan_1080) != render_plan_digest(plan_1440)

    # Recompilation with the same semantic inputs keeps the existing PR7/PR17 cache key stable.
    repeated = compile_timeline(
        project,
        {},
        profile_id=LONG_FORM_1080P_PROFILE_ID,
    )
    assert render_plan_digest(repeated) == render_plan_digest(plan_1080)
