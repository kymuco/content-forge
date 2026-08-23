from __future__ import annotations

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    NormalizedRect,
    OutputProfile,
    Project,
    Scene,
    TemplateRef,
    Variant,
    new_entity_id,
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
        sha256="c" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=123,
        width=720,
        height=1280,
        has_audio=False,
    )


def _project(asset: Asset, profile: OutputProfile) -> Project:
    return Project(
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        variants=(Variant(language="en", hook="Edge-safe hook"),),
        scenes=(
            Scene(
                order=0,
                duration_seconds=2.0,
                media=AssetRef(asset_id=asset.asset_id),
            ),
        ),
        output_profiles=(profile,),
    )


def test_text_decoration_cannot_expand_beyond_output_canvas() -> None:
    asset = _image_asset()
    profile = OutputProfile(
        profile_id="edge_vertical",
        width=540,
        height=960,
        fps=30,
        safe_zones=(),
    )
    project = _project(asset, profile)
    config = HookOverlayConfig(
        hook_region=NormalizedRect(x=0.0, y=0.10, width=0.80, height=0.19),
        border_width_ratio=0.03,
    )

    with pytest.raises(HookOverlayTemplateError, match="exceeds output canvas"):
        resolve_hook_overlay(project, {asset.asset_id: asset}, config=config)


def test_canvas_edge_is_allowed_when_no_decoration_expands_outside() -> None:
    asset = _image_asset()
    profile = OutputProfile(
        profile_id="edge_vertical",
        width=540,
        height=960,
        fps=30,
        safe_zones=(),
    )
    project = _project(asset, profile)
    config = HookOverlayConfig(
        hook_region=NormalizedRect(x=0.0, y=0.10, width=0.80, height=0.19),
        border_width_ratio=0.0,
        box=False,
    )

    resolved = resolve_hook_overlay(project, {asset.asset_id: asset}, config=config)

    assert resolved.overlays[0].placement == config.hook_region


def test_hook_region_that_collapses_after_edge_rounding_fails_closed() -> None:
    asset = _image_asset()
    profile = OutputProfile(
        profile_id="tiny_preview",
        width=540,
        height=960,
        fps=30,
        safe_zones=(),
    )
    project = _project(asset, profile)
    variant = project.variants[0].validated_copy(update={"hook": "W"})
    project = project.validated_copy(update={"variants": (variant,)})
    config = HookOverlayConfig(
        hook_region=NormalizedRect(x=0.10, y=0.10, width=0.0005, height=0.19),
        font_size_ratio=0.0004,
        border_width_ratio=0.0,
        box=False,
    )

    with pytest.raises(HookOverlayTemplateError, match="collapses after output-profile pixel rounding"):
        resolve_hook_overlay(project, {asset.asset_id: asset}, config=config)


def test_positive_edge_decoration_has_same_preview_and_final_rejection() -> None:
    asset = _image_asset()
    preview = OutputProfile(
        profile_id="edge_preview",
        width=540,
        height=960,
        fps=30,
        safe_zones=(),
    )
    final = OutputProfile(
        profile_id="edge_final",
        width=1080,
        height=1920,
        fps=30,
        safe_zones=(),
    )
    project = _project(asset, preview).validated_copy(
        update={"output_profiles": (preview, final)}
    )
    config = HookOverlayConfig(
        hook_region=NormalizedRect(x=0.0, y=0.10, width=0.80, height=0.19),
        border_width_ratio=0.0005,
    )

    for profile in (preview, final):
        with pytest.raises(HookOverlayTemplateError, match="exceeds output canvas"):
            resolve_hook_overlay(
                project,
                {asset.asset_id: asset},
                profile_id=profile.profile_id,
                config=config,
            )