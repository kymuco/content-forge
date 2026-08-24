from __future__ import annotations

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    NormalizedRect,
    Project,
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
    resolve_hook_overlay,
)


def _image_asset() -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="d" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=123,
        width=720,
        height=1280,
        has_audio=False,
    )


def _project(asset: Asset, *, hook: str) -> Project:
    return Project(
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        variants=(Variant(language="en", hook=hook),),
        scenes=(
            Scene(
                order=0,
                duration_seconds=2.0,
                media=AssetRef(asset_id=asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(), shorts_final_profile()),
    )


def test_small_font_ratio_keeps_preview_and_final_wrapping_identical() -> None:
    asset = _image_asset()
    project = _project(asset, hook="W" * 36)
    variant = project.variants[0].validated_copy(
        update={"style_overrides": {"hook_overlay.font_size_ratio": 0.02}}
    )
    project = project.validated_copy(update={"variants": (variant,)})
    assets = {asset.asset_id: asset}

    preview = resolve_hook_overlay(
        project,
        assets,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
    )
    final = resolve_hook_overlay(
        project,
        assets,
        profile_id=SHORTS_FINAL_PROFILE_ID,
    )

    assert preview.overlays[0].text == final.overlays[0].text
    assert preview.properties["hook_wrap_width_chars"] == final.properties[
        "hook_wrap_width_chars"
    ]
    assert preview.properties["hook_wrap_width_chars"] == 39
    assert preview.overlays[0].properties["font_size"] == 11
    assert final.overlays[0].properties["font_size"] == 22
    assert preview.properties["hook_required_width_pixels"] <= preview.properties[
        "hook_region_width_pixels"
    ]
    assert final.properties["hook_required_width_pixels"] <= final.properties[
        "hook_region_width_pixels"
    ]


def test_preview_fails_if_final_pixel_rounding_would_overflow_same_layout() -> None:
    asset = _image_asset()
    project = _project(asset, hook="W" * 60)
    config = HookOverlayConfig(
        font_size_ratio=0.0127,
        border_width_ratio=0.0064,
    )

    with pytest.raises(HookOverlayTemplateError, match=SHORTS_FINAL_PROFILE_ID):
        resolve_hook_overlay(
            project,
            {asset.asset_id: asset},
            profile_id=SHORTS_PREVIEW_PROFILE_ID,
            config=config,
        )


def test_text_decoration_expansion_cannot_enter_touching_safe_zone() -> None:
    asset = _image_asset()
    project = _project(asset, hook="Safe hook")
    config = HookOverlayConfig(
        hook_region=NormalizedRect(x=0.06, y=0.045, width=0.80, height=0.19),
        border_width_ratio=0.005,
    )

    with pytest.raises(HookOverlayTemplateError, match="protected output safe zone"):
        resolve_hook_overlay(
            project,
            {asset.asset_id: asset},
            profile_id=SHORTS_FINAL_PROFILE_ID,
            config=config,
        )
