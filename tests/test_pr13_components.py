from __future__ import annotations

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    NormalizedPoint,
    NormalizedRect,
    Project,
    new_entity_id,
)
from content_forge.profiles import shorts_final_profile, shorts_preview_profile
from content_forge.templates import (
    ARTIST_CREDIT_COMPONENT_ID,
    AVATAR_COMPONENT_ID,
    BLUR_REVEAL_COMPONENT_ID,
    COMMENT_CARD_COMPONENT_ID,
    CROP_REVEAL_COMPONENT_ID,
    KEN_BURNS_COMPONENT_ID,
    PAN_COMPONENT_ID,
    PR13_COMPONENTS,
    PR13_COMPONENT_VERSION,
    REACTION_COMPONENT_ID,
    TRANSITION_COMPONENT_ID,
    WATERMARK_COMPONENT_ID,
    ComponentRuntimeError,
    artist_credit_overlay,
    avatar_overlay,
    blur_reveal_motion,
    comment_card_overlay,
    crop_reveal_motion,
    create_builtin_registries,
    ken_burns_motion,
    pan_motion,
    reaction_overlay,
    simple_transition,
    watermark_overlay,
)


def _image(*, width: int = 1000, height: int = 1500, sha: str = "a") -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256=sha * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=100,
        width=width,
        height=height,
        has_audio=False,
    )


def _video(*, duration: float | None, sha: str = "b") -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256=sha * 64,
        media_type=MediaType.VIDEO,
        mime_type="video/mp4",
        size_bytes=100,
        width=720,
        height=1280,
        duration_seconds=duration,
        fps=30.0,
        has_audio=False,
    )


def _project() -> Project:
    return Project(
        content_kind="component_fixture",
        output_profiles=(shorts_preview_profile(), shorts_final_profile()),
    )


def test_pr13_component_definitions_are_registered_exactly_once() -> None:
    registry = create_builtin_registries().components
    expected = {
        ARTIST_CREDIT_COMPONENT_ID,
        COMMENT_CARD_COMPONENT_ID,
        REACTION_COMPONENT_ID,
        AVATAR_COMPONENT_ID,
        WATERMARK_COMPONENT_ID,
        KEN_BURNS_COMPONENT_ID,
        PAN_COMPONENT_ID,
        CROP_REVEAL_COMPONENT_ID,
        BLUR_REVEAL_COMPONENT_ID,
        TRANSITION_COMPONENT_ID,
    }
    registered = {
        item.component_id
        for item in registry.definitions()
        if item.version == PR13_COMPONENT_VERSION
    }
    assert expected <= registered
    assert len(PR13_COMPONENTS) == len(expected)
    assert len({item.component_id for item in PR13_COMPONENTS}) == len(PR13_COMPONENTS)


def test_bounded_credit_and_comment_preflight_all_project_profiles() -> None:
    project = _project()
    preview = shorts_preview_profile()
    credit = artist_credit_overlay(
        project,
        preview,
        credit_text="Artist: Synthetic Fixture",
        placement=NormalizedRect(x=0.05, y=0.82, width=0.80, height=0.06),
    )
    assert credit.component_type == ARTIST_CREDIT_COMPONENT_ID
    assert credit.properties["layout_required_width_pixels"] <= credit.properties[
        "layout_region_width_pixels"
    ]

    source_id = new_entity_id(EntityKind.SOURCE)
    comment = comment_card_overlay(
        project,
        preview,
        display_label="@fixture",
        comment_text="This card remains bounded and provenance-explicit.",
        placement=NormalizedRect(x=0.07, y=0.62, width=0.76, height=0.18),
        provenance="source",
        source_id=source_id,
    )
    assert comment.properties["comment_source_id"] == source_id
    with pytest.raises(ComponentRuntimeError, match="requires source_id"):
        comment_card_overlay(
            project,
            preview,
            display_label="@fixture",
            comment_text="Missing provenance fails closed.",
            placement=NormalizedRect(x=0.07, y=0.62, width=0.76, height=0.18),
            provenance="source",
        )


def test_text_overflow_fails_closed() -> None:
    with pytest.raises(ComponentRuntimeError, match="overflows"):
        artist_credit_overlay(
            _project(),
            shorts_preview_profile(),
            credit_text=" ".join(["extremelylongcredit"] * 40),
            placement=NormalizedRect(x=0.05, y=0.82, width=0.12, height=0.02),
        )


def test_avatar_reaction_and_watermark_preserve_roles() -> None:
    project = _project()
    preview = shorts_preview_profile()
    image = _image(width=800, height=800)
    ref = AssetRef(asset_id=image.asset_id)

    avatar = avatar_overlay(
        project,
        preview,
        asset=image,
        asset_ref=ref,
        cell=NormalizedRect(x=0.05, y=0.05, width=0.18, height=0.12),
    )
    assert avatar.asset_ref is not None and avatar.asset_ref.role == "avatar"

    reaction = reaction_overlay(
        project,
        preview,
        asset=image,
        asset_ref=ref,
        cell=NormalizedRect(x=0.08, y=0.65, width=0.72, height=0.20),
        duration_seconds=2.0,
    )
    assert reaction.asset_ref is not None and reaction.asset_ref.role == "reaction"

    watermark = watermark_overlay(
        project,
        preview,
        placement=NormalizedRect(x=0.68, y=0.04, width=0.20, height=0.05),
        text="@contentforge",
    )
    assert watermark.component_type == WATERMARK_COMPONENT_ID
    with pytest.raises(ComponentRuntimeError, match="exactly one"):
        watermark_overlay(
            project,
            preview,
            placement=NormalizedRect(x=0.68, y=0.04, width=0.20, height=0.05),
            text="bad",
            asset=image,
            asset_ref=ref,
        )


def test_reaction_video_bounds_and_loop_fail_closed() -> None:
    project = _project()
    preview = shorts_preview_profile()
    short = _video(duration=1.0)
    with pytest.raises(ComponentRuntimeError, match="shorter"):
        reaction_overlay(
            project,
            preview,
            asset=short,
            asset_ref=AssetRef(asset_id=short.asset_id),
            cell=NormalizedRect(x=0.08, y=0.65, width=0.72, height=0.20),
            duration_seconds=2.0,
        )
    long = _video(duration=3.0, sha="c")
    with pytest.raises(ComponentRuntimeError, match="looping is not supported"):
        reaction_overlay(
            project,
            preview,
            asset=long,
            asset_ref=AssetRef(asset_id=long.asset_id),
            cell=NormalizedRect(x=0.08, y=0.65, width=0.72, height=0.20),
            duration_seconds=2.0,
            loop=True,
        )


def test_motion_helpers_emit_canonical_specs_and_transition_set() -> None:
    asset = _image()
    profile = shorts_preview_profile()
    placement = NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0)
    slow = ken_burns_motion(asset, profile, placement)
    pan = pan_motion(
        asset,
        profile,
        placement,
        start_focus=NormalizedPoint(x=0.3, y=0.5),
        end_focus=NormalizedPoint(x=0.7, y=0.5),
    )
    reveal = crop_reveal_motion(asset, profile, placement)
    blur = blur_reveal_motion(reveal_duration_seconds=0.4)
    assert slow.motion_type == "slow_zoom"
    assert pan.motion_type == "pan"
    assert reveal.motion_type == "crop_reveal"
    assert blur.motion_type == "blur_reveal"
    for item in (slow, pan, reveal):
        assert item.start_rect is not None and item.end_rect is not None
        assert item.start_rect.width / item.start_rect.height == pytest.approx(
            item.end_rect.width / item.end_rect.height
        )
    transition = simple_transition("crossfade", duration_seconds=0.2)
    assert transition.transition_type == "crossfade"
    with pytest.raises(ComponentRuntimeError, match="unsupported"):
        simple_transition("magic_spin")
