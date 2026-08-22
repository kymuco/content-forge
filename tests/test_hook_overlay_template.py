from __future__ import annotations

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    FitMode,
    MediaType,
    NormalizedRect,
    OutputProfile,
    Project,
    SafeZone,
    Scene,
    TemplateRef,
    Variant,
    new_entity_id,
)
from content_forge.profiles import (
    SHORTS_FINAL_PROFILE_ID,
    SHORTS_PREVIEW_PROFILE_ID,
    shorts_final_profile,
    shorts_preview_profile,
)
from content_forge.templates import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    HookOverlayConfig,
    HookOverlayTemplateError,
    compile_hook_overlay,
    resolve_hook_overlay,
)
from content_forge.timeline import (
    TemplateResolutionError,
    compile_timeline,
    render_plan_digest,
)


def image_asset(*, has_audio: bool | None = False) -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="a" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=123,
        width=720,
        height=1280,
        has_audio=has_audio,
    )


def video_asset(*, has_audio: bool | None = True) -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="b" * 64,
        media_type=MediaType.VIDEO,
        mime_type="video/mp4",
        size_bytes=456,
        width=720,
        height=1280,
        duration_seconds=12.0,
        fps=30.0,
        has_audio=has_audio,
    )


def project_for(
    asset: Asset,
    *,
    hook: str = "This tiny detail changes the whole scene",
    profiles: tuple[OutputProfile, ...] | None = None,
) -> Project:
    variant = Variant(language="en", hook=hook)
    return Project(
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        variants=(variant,),
        scenes=(
            Scene(
                order=0,
                duration_seconds=2.0,
                media=AssetRef(asset_id=asset.asset_id),
            ),
        ),
        output_profiles=profiles or (shorts_preview_profile(), shorts_final_profile()),
    )


def test_builtin_shorts_profiles_keep_preview_and_final_geometry_in_lockstep() -> None:
    preview = shorts_preview_profile()
    final = shorts_final_profile()

    assert preview.profile_id == SHORTS_PREVIEW_PROFILE_ID
    assert final.profile_id == SHORTS_FINAL_PROFILE_ID
    assert (preview.width, preview.height) == (540, 960)
    assert (final.width, final.height) == (1080, 1920)
    assert preview.safe_zones == final.safe_zones
    assert {zone.name for zone in final.safe_zones} == {"top_ui", "right_ui", "bottom_ui"}


def test_hook_overlay_resolves_fullscreen_media_wrapped_hook_and_original_audio() -> None:
    asset = video_asset(has_audio=True)
    project = project_for(asset)

    resolved = resolve_hook_overlay(
        project,
        {asset.asset_id: asset},
        profile_id=SHORTS_FINAL_PROFILE_ID,
    )

    assert resolved.template_id == HOOK_OVERLAY_TEMPLATE_ID
    assert resolved.version == HOOK_OVERLAY_TEMPLATE_VERSION
    assert resolved.scenes is not None
    assert resolved.scenes[0].placement == NormalizedRect(x=0, y=0, width=1, height=1)
    assert resolved.scenes[0].fit_mode is FitMode.COVER
    assert len(resolved.scenes[0].audio_tracks) == 1
    assert resolved.scenes[0].audio_tracks[0].track_type == "original"
    assert len(resolved.overlays) == 1
    assert resolved.overlays[0].component_type == "text"
    assert resolved.overlays[0].text
    assert resolved.overlays[0].properties["font_size"] == 63
    assert resolved.properties["resolved_profile_id"] == SHORTS_FINAL_PROFILE_ID
    assert resolved.properties["auto_original_audio_tracks"] == 1
    assert resolved.properties["hook_required_height_pixels"] <= resolved.properties[
        "hook_region_height_pixels"
    ]


def test_template_generated_ids_and_wrapping_are_deterministic() -> None:
    asset = video_asset(has_audio=True)
    project = project_for(asset, hook="One two three four five six seven eight")
    assets = {asset.asset_id: asset}

    first = resolve_hook_overlay(project, assets, profile_id=SHORTS_PREVIEW_PROFILE_ID)
    second = resolve_hook_overlay(project, assets, profile_id=SHORTS_PREVIEW_PROFILE_ID)

    assert first == second
    assert first.overlays[0].overlay_id == second.overlays[0].overlay_id
    assert first.scenes is not None and second.scenes is not None
    assert first.scenes[0].audio_tracks[0].audio_track_id == second.scenes[0].audio_tracks[0].audio_track_id


def test_preview_and_final_scale_typography_but_keep_semantic_ids_and_line_breaks() -> None:
    asset = image_asset()
    project = project_for(asset, hook="Deterministic wrapping stays equal")
    assets = {asset.asset_id: asset}

    preview = resolve_hook_overlay(project, assets, profile_id=SHORTS_PREVIEW_PROFILE_ID)
    final = resolve_hook_overlay(project, assets, profile_id=SHORTS_FINAL_PROFILE_ID)

    assert preview.overlays[0].overlay_id == final.overlays[0].overlay_id
    assert preview.overlays[0].text == final.overlays[0].text
    assert preview.overlays[0].placement == final.overlays[0].placement
    assert preview.overlays[0].properties["font_size"] == 31
    assert final.overlays[0].properties["font_size"] == 63


def test_wide_ascii_glyphs_use_conservative_full_em_wrap_budget() -> None:
    asset = image_asset()
    project = project_for(asset, hook="W" * 40)

    resolved = resolve_hook_overlay(
        project,
        {asset.asset_id: asset},
        profile_id=SHORTS_FINAL_PROFILE_ID,
    )

    lines = (resolved.overlays[0].text or "").split("\n")
    wrap_width = resolved.properties["hook_wrap_width_chars"]
    assert wrap_width == 13
    assert len(lines) == 4
    assert all(len(line) <= wrap_width for line in lines)


def test_cjk_and_emoji_fail_closed_until_font_backed_text_pipeline() -> None:
    asset = image_asset()
    for hook in ("別のフック", "Look 👀 here"):
        project = project_for(asset, hook=hook)
        with pytest.raises(HookOverlayTemplateError, match="font-backed text pipeline"):
            resolve_hook_overlay(
                project,
                {asset.asset_id: asset},
                profile_id=SHORTS_PREVIEW_PROFILE_ID,
            )


def test_glyph_safety_budget_cannot_be_reduced_below_one_em() -> None:
    with pytest.raises(ValueError):
        HookOverlayConfig(max_glyph_width_em=0.9)


def test_line_height_safety_budget_cannot_be_reduced() -> None:
    with pytest.raises(ValueError):
        HookOverlayConfig(line_height_em=1.2)


def test_large_typography_that_cannot_fit_hook_region_fails_closed() -> None:
    asset = image_asset()
    project = project_for(asset, hook="one two three")
    variant = project.variants[0].validated_copy(
        update={"style_overrides": {"hook_overlay.font_size_ratio": 0.2}}
    )
    project = project.validated_copy(update={"variants": (variant,)})

    with pytest.raises(HookOverlayTemplateError, match="hook region height"):
        resolve_hook_overlay(
            project,
            {asset.asset_id: asset},
            profile_id=SHORTS_PREVIEW_PROFILE_ID,
        )


def test_image_scene_does_not_receive_original_audio_track() -> None:
    asset = image_asset()
    project = project_for(asset)

    resolved = resolve_hook_overlay(
        project,
        {asset.asset_id: asset},
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )

    assert resolved.scenes is not None
    assert resolved.scenes[0].audio_tracks == ()
    assert resolved.properties["auto_original_audio_tracks"] == 0


def test_unknown_video_audio_metadata_fails_closed() -> None:
    asset = video_asset(has_audio=None)
    project = project_for(asset)

    with pytest.raises(HookOverlayTemplateError, match="audio metadata is unknown"):
        resolve_hook_overlay(
            project,
            {asset.asset_id: asset},
            profile_id=SHORTS_PREVIEW_PROFILE_ID,
        )


def test_hook_overflow_requires_edit_instead_of_silent_truncation() -> None:
    asset = image_asset()
    project = project_for(asset, hook="word " * 100)

    with pytest.raises(HookOverlayTemplateError, match="template allows"):
        resolve_hook_overlay(
            project,
            {asset.asset_id: asset},
            profile_id=SHORTS_PREVIEW_PROFILE_ID,
        )


def test_variant_style_overrides_are_namespaced_and_revalidated() -> None:
    asset = image_asset()
    project = project_for(asset)
    original = project.variants[0]
    styled = original.validated_copy(
        update={
            "style_overrides": {
                "hook_overlay.font_color": "yellow",
                "hook_overlay.font_size_ratio": 0.05,
                "unrelated_template.foo": "ignored",
            }
        }
    )
    project = project.validated_copy(update={"variants": (styled,)})

    resolved = resolve_hook_overlay(
        project,
        {asset.asset_id: asset},
        profile_id=SHORTS_FINAL_PROFILE_ID,
    )

    assert resolved.overlays[0].properties["font_color"] == "yellow"
    assert resolved.overlays[0].properties["font_size"] == 54


def test_unknown_hook_overlay_style_override_fails_closed() -> None:
    asset = image_asset()
    project = project_for(asset)
    variant = project.variants[0].validated_copy(
        update={"style_overrides": {"hook_overlay.not_a_real_option": 1}}
    )
    project = project.validated_copy(update={"variants": (variant,)})

    with pytest.raises(HookOverlayTemplateError, match="unknown hook_overlay style override"):
        resolve_hook_overlay(
            project,
            {asset.asset_id: asset},
            profile_id=SHORTS_FINAL_PROFILE_ID,
        )


def test_hook_region_cannot_overlap_profile_protected_safe_zone() -> None:
    asset = image_asset()
    unsafe_profile = OutputProfile(
        profile_id="unsafe_vertical",
        width=540,
        height=960,
        fps=30,
        safe_zones=(
            SafeZone(
                name="blocked",
                rect=NormalizedRect(x=0.05, y=0.05, width=0.5, height=0.2),
            ),
        ),
    )
    project = project_for(asset, profiles=(unsafe_profile,))

    with pytest.raises(HookOverlayTemplateError, match="protected output safe zone"):
        resolve_hook_overlay(project, {asset.asset_id: asset})


def test_compile_hook_overlay_binds_variant_profile_and_produces_stable_plan_digest() -> None:
    asset = video_asset(has_audio=True)
    project = project_for(asset)
    assets = {asset.asset_id: asset}

    first = compile_hook_overlay(
        project,
        assets,
        profile_id=SHORTS_FINAL_PROFILE_ID,
    )
    second = compile_hook_overlay(
        project,
        assets,
        profile_id=SHORTS_FINAL_PROFILE_ID,
    )

    assert first.template_id == HOOK_OVERLAY_TEMPLATE_ID
    assert first.template_version == HOOK_OVERLAY_TEMPLATE_VERSION
    assert first.variant_id == project.variants[0].variant_id
    assert first.output_profile.profile_id == SHORTS_FINAL_PROFILE_ID
    assert first.overlays[0].text
    assert first.audio_tracks[0].track_type == "original"
    assert first.audio_tracks[0].asset_id == asset.asset_id
    assert render_plan_digest(first) == render_plan_digest(second)


def test_resolved_template_rejects_cross_profile_and_cross_variant_compilation() -> None:
    asset = image_asset()
    project = project_for(asset, hook="Primary hook")
    primary = project.variants[0]
    alternate = Variant(language="ru", hook="Другой хук")
    project = project.validated_copy(update={"variants": (primary, alternate)})
    assets = {asset.asset_id: asset}

    resolved = resolve_hook_overlay(
        project,
        assets,
        profile_id=SHORTS_FINAL_PROFILE_ID,
        variant_id=primary.variant_id,
    )

    with pytest.raises(TemplateResolutionError, match="profile binding"):
        compile_timeline(
            project,
            assets,
            profile_id=SHORTS_PREVIEW_PROFILE_ID,
            variant_id=primary.variant_id,
            template=resolved,
        )

    with pytest.raises(TemplateResolutionError, match="variant binding"):
        compile_timeline(
            project,
            assets,
            profile_id=SHORTS_FINAL_PROFILE_ID,
            variant_id=alternate.variant_id,
            template=resolved,
        )

    matching = compile_timeline(
        project,
        assets,
        profile_id=SHORTS_FINAL_PROFILE_ID,
        variant_id=primary.variant_id,
        template=resolved,
    )
    assert matching.output_profile.profile_id == SHORTS_FINAL_PROFILE_ID
    assert matching.variant_id == primary.variant_id


def test_resolver_requires_matching_template_reference_and_explicit_variant_selection() -> None:
    asset = image_asset()
    project = project_for(asset)
    wrong = project.validated_copy(
        update={"template": TemplateRef(template_id="hook_topbar", version="1.0")}
    )
    with pytest.raises(HookOverlayTemplateError, match="project template must be"):
        resolve_hook_overlay(wrong, {asset.asset_id: asset}, profile_id=SHORTS_PREVIEW_PROFILE_ID)

    second_variant = Variant(language="ru", hook="Другой хук")
    multi = project.validated_copy(update={"variants": (project.variants[0], second_variant)})
    with pytest.raises(HookOverlayTemplateError, match="explicit variant_id"):
        resolve_hook_overlay(multi, {asset.asset_id: asset}, profile_id=SHORTS_PREVIEW_PROFILE_ID)

    resolved = resolve_hook_overlay(
        multi,
        {asset.asset_id: asset},
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
        variant_id=second_variant.variant_id,
    )
    assert resolved.properties["resolved_variant_id"] == second_variant.variant_id
