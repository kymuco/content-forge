from test_timeline_compiler import build_case

from content_forge.timeline import compile_timeline


def test_basic_render_plan_semantic_snapshot() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)

    snapshot = {
        "version": (plan.render_plan_version, plan.compiler_version),
        "project": (plan.project_id, plan.variant_id, plan.variant_language),
        "template": (plan.template_id, plan.template_version),
        "profile": (
            plan.output_profile.profile_id,
            plan.output_profile.width,
            plan.output_profile.height,
            plan.output_profile.fps,
        ),
        "duration": plan.total_duration_seconds,
        "scenes": [
            {
                "id": scene.scene_id,
                "order": scene.order,
                "time": (scene.start_seconds, scene.duration_seconds, scene.end_seconds),
                "media": scene.media_asset_id,
                "trim": (scene.trim_start_seconds, scene.trim_duration_seconds),
                "transition_in": (
                    scene.transition_in.transition_type,
                    scene.transition_in.duration_seconds,
                ),
                "transition_out": (
                    scene.transition_out.transition_type,
                    scene.transition_out.duration_seconds,
                ),
                "motion": scene.motion_type,
            }
            for scene in plan.scenes
        ],
        "overlays": [
            {
                "id": item.overlay_id,
                "type": item.component_type,
                "scope": item.scope_scene_id,
                "time": (item.start_seconds, item.duration_seconds, item.end_seconds),
                "z": item.z_index,
                "text": item.text,
                "asset": item.asset_id,
            }
            for item in plan.overlays
        ],
        "audio": [
            {
                "id": item.audio_track_id,
                "type": item.track_type,
                "scope": item.scope_scene_id,
                "time": (item.start_seconds, item.duration_seconds, item.end_seconds),
                "source_start": item.source_start_seconds,
                "asset": item.asset_id,
                "gain": item.gain_db,
                "loop": item.loop,
            }
            for item in plan.audio_tracks
        ],
        "assets": [
            (item.asset_id, item.sha256, item.media_type.value) for item in plan.assets
        ],
        "template_properties": dict(plan.template_properties),
    }

    assert snapshot == {
        "version": ("1.0", "1"),
        "project": (
            "cf_project_99999999999999999999999999999999",
            "cf_variant_88888888888888888888888888888888",
            "en",
        ),
        "template": ("test_template", "1"),
        "profile": ("short_vertical", 1080, 1920, 30.0),
        "duration": 6.5,
        "scenes": [
            {
                "id": "cf_scene_44444444444444444444444444444444",
                "order": 0,
                "time": (0.0, 4.0, 4.0),
                "media": "cf_asset_11111111111111111111111111111111",
                "trim": (1.0, None),
                "transition_in": ("cut", 0.0),
                "transition_out": ("crossfade", 0.5),
                "motion": None,
            },
            {
                "id": "cf_scene_77777777777777777777777777777777",
                "order": 1,
                "time": (3.5, 3.0, 6.5),
                "media": "cf_asset_22222222222222222222222222222222",
                "trim": (0.0, None),
                "transition_in": ("crossfade", 0.5),
                "transition_out": ("cut", 0.0),
                "motion": "slow_zoom",
            },
        ],
        "overlays": [
            {
                "id": "cf_overlay_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "type": "image",
                "scope": None,
                "time": (0.0, 6.5, 6.5),
                "z": -10,
                "text": None,
                "asset": "cf_asset_22222222222222222222222222222222",
            },
            {
                "id": "cf_overlay_55555555555555555555555555555555",
                "type": "text",
                "scope": "cf_scene_44444444444444444444444444444444",
                "time": (0.25, 3.75, 4.0),
                "z": 10,
                "text": "This detail is easy to miss",
                "asset": None,
            },
        ],
        "audio": [
            {
                "id": "cf_audio_66666666666666666666666666666666",
                "type": "original",
                "scope": "cf_scene_44444444444444444444444444444444",
                "time": (0.0, 4.0, 4.0),
                "source_start": 1.0,
                "asset": "cf_asset_11111111111111111111111111111111",
                "gain": 0.0,
                "loop": False,
            },
            {
                "id": "cf_audio_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "type": "music",
                "scope": None,
                "time": (0.0, 6.5, 6.5),
                "source_start": 0.0,
                "asset": "cf_asset_33333333333333333333333333333333",
                "gain": -12.0,
                "loop": True,
            },
        ],
        "assets": [
            (
                "cf_asset_11111111111111111111111111111111",
                "1" * 64,
                "video",
            ),
            (
                "cf_asset_22222222222222222222222222222222",
                "2" * 64,
                "image",
            ),
            (
                "cf_asset_33333333333333333333333333333333",
                "3" * 64,
                "audio",
            ),
        ],
        "template_properties": {"skin": "synthetic"},
    }
