import pytest
from pydantic import ValidationError

from content_forge.core import (
    AssetRef,
    AudioTrack,
    EntityKind,
    MediaType,
    NormalizedRect,
    Overlay,
    Scene,
)
from content_forge.timeline import (
    PlannedScene,
    RenderPlan,
    TimelineBoundsError,
    TimelineCompileError,
    compile_timeline,
)

from test_timeline_compiler import asset, build_case, fixed_id


def test_asset_resolver_must_return_requested_identity() -> None:
    project, template, assets = build_case()
    requested = project.scenes[0].media.asset_id
    wrong = asset("c", media_type=MediaType.VIDEO, duration=10.0, has_audio=True)
    broken = dict(assets)
    broken[requested] = wrong

    with pytest.raises(TimelineCompileError, match="asset resolver returned"):
        compile_timeline(project, broken, template=template)


def test_template_can_supply_fully_resolved_scene_graph() -> None:
    project, template, assets = build_case()
    replacement = Scene(
        scene_id=fixed_id(EntityKind.SCENE, "c"),
        order=0,
        duration_seconds=2.0,
        media=project.scenes[1].media,
        placement=NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.8),
    )
    resolved = template.validated_copy(update={"scenes": (replacement,)})

    plan = compile_timeline(project, assets, template=resolved)

    assert len(plan.scenes) == 1
    assert plan.scenes[0].scene_id == replacement.scene_id
    assert plan.scenes[0].placement == replacement.placement
    assert plan.total_duration_seconds == 2.0


def test_template_scene_override_rejects_duplicate_scene_ids() -> None:
    project, template, assets = build_case()
    duplicate_id = fixed_id(EntityKind.SCENE, "c")
    first = Scene(scene_id=duplicate_id, order=0, duration_seconds=1.0)
    second = Scene(scene_id=duplicate_id, order=1, duration_seconds=1.0)
    resolved = template.validated_copy(update={"scenes": (first, second)})

    with pytest.raises(TimelineCompileError, match="scene IDs"):
        compile_timeline(project, assets, template=resolved)


def test_source_trim_shorter_than_scene_is_rejected_even_without_probe_duration() -> None:
    project, template, assets = build_case()
    video_id = project.scenes[0].media.asset_id
    unknown_duration = asset(
        "1", media_type=MediaType.VIDEO, duration=None, has_audio=True
    )
    assert unknown_duration.asset_id == video_id
    changed_assets = dict(assets)
    changed_assets[video_id] = unknown_duration
    changed_scene = project.scenes[0].validated_copy(
        update={"trim_duration_seconds": 2.0}
    )
    changed_project = project.validated_copy(
        update={"scenes": (changed_scene, project.scenes[1])}
    )

    with pytest.raises(TimelineBoundsError, match="shorter than scene duration"):
        compile_timeline(changed_project, changed_assets, template=template)


def test_visual_overlay_rejects_audio_only_asset() -> None:
    project, template, assets = build_case()
    music_id = fixed_id(EntityKind.ASSET, "3")
    overlay = Overlay(
        component_type="image",
        placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
        asset_ref=AssetRef(asset_id=music_id),
    )
    resolved = template.validated_copy(update={"overlays": (overlay,)})

    with pytest.raises(TimelineCompileError, match="audio-only"):
        compile_timeline(project, assets, template=resolved)


def test_audio_track_rejects_image_asset_even_when_has_audio_is_unknown() -> None:
    project, template, assets = build_case()
    image_id = fixed_id(EntityKind.ASSET, "2")
    track = AudioTrack(
        track_type="music",
        asset_ref=AssetRef(asset_id=image_id),
        loop=True,
    )
    changed = project.validated_copy(update={"audio_tracks": (track,)})

    with pytest.raises(TimelineCompileError, match="no audio"):
        compile_timeline(changed, assets, template=template)


def test_render_plan_model_rejects_missing_asset_table_entry() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    payload["assets"] = ()

    with pytest.raises(ValidationError, match="missing planned asset"):
        RenderPlan.model_validate(payload)


def test_planned_scene_model_rejects_inconsistent_end_time() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    scene = plan.scenes[0]
    payload = scene.model_dump(mode="python", round_trip=True)
    payload["end_seconds"] = scene.end_seconds + 1.0

    with pytest.raises(ValidationError, match=r"start \+ duration"):
        PlannedScene.model_validate(payload)
