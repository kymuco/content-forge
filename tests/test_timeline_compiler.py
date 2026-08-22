from datetime import datetime, timezone

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    AudioTrack,
    EntityKind,
    MediaType,
    MotionSpec,
    NormalizedPoint,
    NormalizedRect,
    OutputProfile,
    Overlay,
    Project,
    Scene,
    TemplateRef,
    TransitionSpec,
    Variant,
)
from content_forge.timeline import (
    MissingTimelineAssetError,
    ResolvedTemplate,
    TemplateResolutionError,
    TimelineBoundsError,
    TimelineCompileError,
    TimelineSelectionError,
    TimelineTransitionError,
    compile_timeline,
    render_plan_digest,
)


def fixed_id(kind: EntityKind, digit: str) -> str:
    return f"cf_{kind.value}_{digit * 32}"


def asset(
    digit: str,
    *,
    media_type: MediaType,
    duration: float | None = None,
    has_audio: bool | None = None,
) -> Asset:
    extension = {
        MediaType.VIDEO: "video/mp4",
        MediaType.IMAGE: "image/png",
        MediaType.AUDIO: "audio/wav",
        MediaType.OTHER: "application/octet-stream",
    }[media_type]
    return Asset(
        asset_id=fixed_id(EntityKind.ASSET, digit),
        sha256=digit * 64,
        media_type=media_type,
        mime_type=extension,
        size_bytes=100,
        width=None if media_type is MediaType.AUDIO else 1920,
        height=None if media_type is MediaType.AUDIO else 1080,
        duration_seconds=duration,
        has_audio=has_audio,
        storage_key=f"assets/{digit}",
        created_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )


def build_case() -> tuple[Project, ResolvedTemplate, dict[str, Asset]]:
    video = asset("1", media_type=MediaType.VIDEO, duration=10.0, has_audio=True)
    image = asset("2", media_type=MediaType.IMAGE)
    music = asset("3", media_type=MediaType.AUDIO, duration=20.0, has_audio=True)

    transition = TransitionSpec(
        transition_type="crossfade",
        duration_seconds=0.5,
        properties={"curve": "linear"},
    )
    scene_one = Scene(
        scene_id=fixed_id(EntityKind.SCENE, "4"),
        order=0,
        duration_seconds=4.0,
        media=AssetRef(asset_id=video.asset_id),
        trim_start_seconds=1.0,
        transition_out=transition,
        overlays=(
            Overlay(
                overlay_id=fixed_id(EntityKind.OVERLAY, "5"),
                component_type="text",
                start_seconds=0.25,
                placement=NormalizedRect(x=0.1, y=0.05, width=0.8, height=0.15),
                variant_field="hook",
                z_index=10,
            ),
        ),
        audio_tracks=(
            AudioTrack(
                audio_track_id=fixed_id(EntityKind.AUDIO, "6"),
                track_type="original",
            ),
        ),
    )
    scene_two = Scene(
        scene_id=fixed_id(EntityKind.SCENE, "7"),
        order=1,
        duration_seconds=3.0,
        media=AssetRef(asset_id=image.asset_id),
        transition_in=transition,
        motion=MotionSpec(
            motion_type="slow_zoom",
            start_rect=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
            end_rect=NormalizedRect(x=0.05, y=0.05, width=0.9, height=0.9),
            focus=NormalizedPoint(x=0.5, y=0.4),
        ),
    )

    variant = Variant(
        variant_id=fixed_id(EntityKind.VARIANT, "8"),
        language="en",
        hook="This detail is easy to miss",
    )
    project = Project(
        project_id=fixed_id(EntityKind.PROJECT, "9"),
        content_kind="character_moment",
        variants=(variant,),
        template=TemplateRef(template_id="test_template", version="1"),
        scenes=(scene_one, scene_two),
        audio_tracks=(
            AudioTrack(
                audio_track_id=fixed_id(EntityKind.AUDIO, "a"),
                track_type="music",
                asset_ref=AssetRef(asset_id=music.asset_id),
                gain_db=-12.0,
                loop=True,
            ),
        ),
        output_profiles=(
            OutputProfile(
                profile_id="short_vertical",
                width=1080,
                height=1920,
                fps=30.0,
            ),
        ),
        created_at=datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc),
    )
    template = ResolvedTemplate(
        template_id="test_template",
        version="1",
        overlays=(
            Overlay(
                overlay_id=fixed_id(EntityKind.OVERLAY, "b"),
                component_type="image",
                placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
                asset_ref=AssetRef(asset_id=image.asset_id),
                z_index=-10,
                properties={"role": "frame"},
            ),
        ),
        properties={"skin": "synthetic"},
    )
    return project, template, {
        video.asset_id: video,
        image.asset_id: image,
        music.asset_id: music,
    }


def test_compile_timeline_resolves_absolute_timing_and_variant_text() -> None:
    project, template, assets = build_case()
    plan = compile_timeline(project, assets, template=template)

    assert plan.total_duration_seconds == 6.5
    assert [(scene.start_seconds, scene.end_seconds) for scene in plan.scenes] == [
        (0.0, 4.0),
        (3.5, 6.5),
    ]
    assert plan.scenes[0].transition_out.transition_type == "crossfade"
    assert plan.scenes[1].transition_in.duration_seconds == 0.5
    assert plan.scenes[1].motion_type == "slow_zoom"

    hook = next(item for item in plan.overlays if item.component_type == "text")
    assert hook.text == "This detail is easy to miss"
    assert hook.start_seconds == 0.25
    assert hook.duration_seconds == 3.75
    assert hook.end_seconds == 4.0
    assert hook.scope_scene_id == project.scenes[0].scene_id

    frame = next(item for item in plan.overlays if item.component_type == "image")
    assert frame.start_seconds == 0.0
    assert frame.end_seconds == 6.5
    assert frame.scope_scene_id is None

    original = next(item for item in plan.audio_tracks if item.track_type == "original")
    assert original.asset_id == project.scenes[0].media.asset_id
    assert original.source_start_seconds == 1.0
    assert original.duration_seconds == 4.0

    assert [item.asset_id for item in plan.assets] == sorted(assets)
    assert plan.template_properties["skin"] == "synthetic"


def test_compilation_and_digest_are_deterministic() -> None:
    project, template, assets = build_case()

    first = compile_timeline(project, assets, template=template)
    second = compile_timeline(project, assets, template=template)

    assert second == first
    assert render_plan_digest(second) == render_plan_digest(first)
    assert len(render_plan_digest(first)) == 64


def test_template_reference_must_be_resolved_exactly() -> None:
    project, template, assets = build_case()

    with pytest.raises(TemplateResolutionError, match="must be resolved"):
        compile_timeline(project, assets)

    wrong = template.validated_copy(update={"version": "2"})
    with pytest.raises(TemplateResolutionError, match="version"):
        compile_timeline(project, assets, template=wrong)


def test_profile_and_variant_selection_are_explicit_when_ambiguous() -> None:
    project, template, assets = build_case()
    second_profile = OutputProfile(
        profile_id="preview_vertical", width=540, height=960, fps=30.0
    )
    second_variant = Variant(language="ja", hook="synthetic")
    ambiguous = project.validated_copy(
        update={
            "output_profiles": (*project.output_profiles, second_profile),
            "variants": (*project.variants, second_variant),
        }
    )

    with pytest.raises(TimelineSelectionError, match="profile_id"):
        compile_timeline(ambiguous, assets, template=template)

    with pytest.raises(TimelineSelectionError, match="variant_id"):
        compile_timeline(
            ambiguous,
            assets,
            template=template,
            profile_id="short_vertical",
        )

    plan = compile_timeline(
        ambiguous,
        assets,
        template=template,
        profile_id="preview_vertical",
        variant_id=second_variant.variant_id,
    )
    assert plan.output_profile.profile_id == "preview_vertical"
    assert plan.variant_id == second_variant.variant_id


def test_missing_asset_fails_before_render_backend() -> None:
    project, template, assets = build_case()
    missing = dict(assets)
    missing.pop(project.scenes[0].media.asset_id)

    with pytest.raises(MissingTimelineAssetError):
        compile_timeline(project, missing, template=template)


def test_scene_orders_must_be_contiguous() -> None:
    project, template, assets = build_case()
    second = project.scenes[1].validated_copy(update={"order": 2})
    invalid = project.validated_copy(update={"scenes": (project.scenes[0], second)})

    with pytest.raises(TimelineCompileError, match="contiguous"):
        compile_timeline(invalid, assets, template=template)


def test_transition_declarations_must_agree() -> None:
    project, template, assets = build_case()
    second = project.scenes[1].validated_copy(
        update={
            "transition_in": TransitionSpec(
                transition_type="crossfade", duration_seconds=0.25
            )
        }
    )
    invalid = project.validated_copy(update={"scenes": (project.scenes[0], second)})

    with pytest.raises(TimelineTransitionError, match="disagreement"):
        compile_timeline(invalid, assets, template=template)


def test_transition_overlap_cannot_consume_an_entire_middle_scene_twice() -> None:
    project, template, assets = build_case()
    first = project.scenes[0].validated_copy(
        update={
            "duration_seconds": 3.0,
            "transition_out": TransitionSpec(
                transition_type="crossfade", duration_seconds=2.0
            ),
        }
    )
    middle = Scene(
        order=1,
        duration_seconds=3.0,
        media=project.scenes[1].media,
        transition_in=TransitionSpec(
            transition_type="crossfade", duration_seconds=2.0
        ),
        transition_out=TransitionSpec(
            transition_type="crossfade", duration_seconds=2.0
        ),
    )
    last = Scene(
        order=2,
        duration_seconds=3.0,
        media=project.scenes[1].media,
        transition_in=TransitionSpec(
            transition_type="crossfade", duration_seconds=2.0
        ),
    )
    invalid = project.validated_copy(update={"scenes": (first, middle, last)})

    with pytest.raises(TimelineTransitionError, match="consume"):
        compile_timeline(invalid, assets, template=template)


def test_overlay_and_audio_cannot_escape_their_scope() -> None:
    project, template, assets = build_case()
    bad_overlay = Overlay(
        component_type="text",
        start_seconds=3.5,
        duration_seconds=1.0,
        placement=NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.2),
        text="too late",
    )
    scene = project.scenes[0].validated_copy(update={"overlays": (bad_overlay,)})
    invalid = project.validated_copy(update={"scenes": (scene, project.scenes[1])})

    with pytest.raises(TimelineBoundsError, match="overlay exceeds"):
        compile_timeline(invalid, assets, template=template)

    bad_audio = AudioTrack(
        track_type="music",
        asset_ref=AssetRef(asset_id=fixed_id(EntityKind.ASSET, "3")),
        start_seconds=3.0,
        duration_seconds=2.0,
    )
    scene = project.scenes[0].validated_copy(update={"audio_tracks": (bad_audio,)})
    invalid = project.validated_copy(update={"scenes": (scene, project.scenes[1])})

    with pytest.raises(TimelineBoundsError, match="audio track exceeds"):
        compile_timeline(invalid, assets, template=template)


def test_video_trim_cannot_run_past_known_asset_duration() -> None:
    project, template, assets = build_case()
    scene = project.scenes[0].validated_copy(
        update={"trim_start_seconds": 8.0, "trim_duration_seconds": 4.0}
    )
    invalid = project.validated_copy(update={"scenes": (scene, project.scenes[1])})

    with pytest.raises(TimelineBoundsError, match="asset duration"):
        compile_timeline(invalid, assets, template=template)


def test_image_scene_rejects_meaningless_source_trim() -> None:
    project, template, assets = build_case()
    scene = project.scenes[1].validated_copy(update={"trim_start_seconds": 0.5})
    invalid = project.validated_copy(update={"scenes": (project.scenes[0], scene)})

    with pytest.raises(TimelineCompileError, match="image scenes"):
        compile_timeline(invalid, assets, template=template)


def test_template_contributions_cannot_reuse_project_entity_ids() -> None:
    project, template, assets = build_case()
    collision = Overlay(
        overlay_id=project.scenes[0].overlays[0].overlay_id,
        component_type="image",
        placement=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
    )
    bad_template = template.validated_copy(update={"overlays": (collision,)})

    with pytest.raises(TimelineCompileError, match="duplicate overlay ID"):
        compile_timeline(project, assets, template=bad_template)
