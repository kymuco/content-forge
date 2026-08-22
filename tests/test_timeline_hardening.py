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


def test_scene_rejects_non_visual_other_asset() -> None:
    project, template, assets = build_case()
    video_id = project.scenes[0].media.asset_id
    unknown = asset("1", media_type=MediaType.OTHER)
    assert unknown.asset_id == video_id
    changed_assets = dict(assets)
    changed_assets[video_id] = unknown

    with pytest.raises(TimelineCompileError, match="video or image"):
        compile_timeline(project, changed_assets, template=template)


def test_visual_overlay_rejects_audio_only_asset() -> None:
    project, template, assets = build_case()
    music_id = fixed_id(EntityKind.ASSET, "3")
    overlay = Overlay(
        component_type="image",
        placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
        asset_ref=AssetRef(asset_id=music_id),
    )
    resolved = template.validated_copy(update={"overlays": (overlay,)})

    with pytest.raises(TimelineCompileError, match="video or image"):
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


def test_reconstructed_render_plan_rejects_scene_schedule_gap() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    second = dict(payload["scenes"][1])
    second["start_seconds"] += 0.25
    second["end_seconds"] += 0.25
    payload["scenes"] = (payload["scenes"][0], second)
    payload["total_duration_seconds"] += 0.25

    with pytest.raises(ValidationError, match="previous end minus transition"):
        RenderPlan.model_validate(payload)


def test_reconstructed_scene_scoped_overlay_must_stay_inside_scene() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    overlay_index = next(
        index
        for index, item in enumerate(payload["overlays"])
        if item["scope_scene_id"] == project.scenes[0].scene_id
    )
    overlays = list(payload["overlays"])
    changed = dict(overlays[overlay_index])
    changed["start_seconds"] = 4.1
    changed["duration_seconds"] = 1.0
    changed["end_seconds"] = 5.1
    overlays[overlay_index] = changed
    payload["overlays"] = tuple(overlays)

    with pytest.raises(ValidationError, match="past its scene scope"):
        RenderPlan.model_validate(payload)


def test_reconstructed_scene_scoped_audio_must_stay_inside_scene() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    track_index = next(
        index
        for index, item in enumerate(payload["audio_tracks"])
        if item["scope_scene_id"] == project.scenes[0].scene_id
    )
    tracks = list(payload["audio_tracks"])
    changed = dict(tracks[track_index])
    changed["start_seconds"] = 4.1
    changed["duration_seconds"] = 1.0
    changed["end_seconds"] = 5.1
    tracks[track_index] = changed
    payload["audio_tracks"] = tuple(tracks)

    with pytest.raises(ValidationError, match="past its scene scope"):
        RenderPlan.model_validate(payload)


def test_reconstructed_plan_rejects_unsupported_versions() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    payload["render_plan_version"] = "2.0"

    with pytest.raises(ValidationError):
        RenderPlan.model_validate(payload)

    payload = plan.model_dump(mode="python", round_trip=True)
    payload["compiler_version"] = "99"
    with pytest.raises(ValidationError):
        RenderPlan.model_validate(payload)


def test_reconstructed_plan_rejects_non_visual_scene_asset() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    video_id = project.scenes[0].media.asset_id
    planned_assets = [dict(item) for item in payload["assets"]]
    target = next(item for item in planned_assets if item["asset_id"] == video_id)
    target["media_type"] = MediaType.OTHER.value
    payload["assets"] = tuple(planned_assets)

    with pytest.raises(ValidationError, match="scene media must be video or image"):
        RenderPlan.model_validate(payload)


def test_reconstructed_plan_rejects_image_as_audio_source() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    image_id = project.scenes[1].media.asset_id
    tracks = [dict(item) for item in payload["audio_tracks"]]
    music = next(item for item in tracks if item["track_type"] == "music")
    music["asset_id"] = image_id
    payload["audio_tracks"] = tuple(tracks)

    with pytest.raises(ValidationError, match="asset with no audio"):
        RenderPlan.model_validate(payload)


def test_reconstructed_plan_rejects_short_scene_trim() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    scenes = [dict(item) for item in payload["scenes"]]
    scenes[0]["trim_duration_seconds"] = 1.0
    payload["scenes"] = tuple(scenes)

    with pytest.raises(ValidationError, match="source trim is shorter than scene"):
        RenderPlan.model_validate(payload)


def test_reconstructed_plan_rejects_image_source_trim() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    scenes = [dict(item) for item in payload["scenes"]]
    scenes[1]["trim_start_seconds"] = 0.25
    payload["scenes"] = tuple(scenes)

    with pytest.raises(ValidationError, match="image scene cannot define source trim"):
        RenderPlan.model_validate(payload)


def test_reconstructed_plan_rejects_unused_asset_table_entries() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)
    payload = plan.model_dump(mode="python", round_trip=True)
    extra = asset("c", media_type=MediaType.IMAGE)
    payload["assets"] = (*payload["assets"], {
        "asset_id": extra.asset_id,
        "sha256": extra.sha256,
        "media_type": extra.media_type.value,
        "mime_type": extra.mime_type,
        "storage_key": extra.storage_key,
        "width": extra.width,
        "height": extra.height,
        "duration_seconds": extra.duration_seconds,
        "has_audio": extra.has_audio,
    })

    with pytest.raises(ValidationError, match="exactly referenced assets"):
        RenderPlan.model_validate(payload)
