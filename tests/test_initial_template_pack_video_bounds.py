from __future__ import annotations

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    Project,
    Scene,
    TemplateRef,
    new_entity_id,
)
from content_forge.profiles import shorts_preview_profile
from content_forge.templates import (
    INITIAL_TEMPLATE_VERSION,
    REACTION_BOTTOM_TEMPLATE_ID,
    SYNC_STACK_TEMPLATE_ID,
    InitialTemplateError,
    compile_registered_template,
)


def _video(*, duration: float | None = 5.0, sha: str = "a") -> Asset:
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


def _sync_project(asset: Asset, *, trim_start: float = 0.0, trim_duration: float | None = None) -> Project:
    return Project(
        content_kind="sync",
        template=TemplateRef(template_id=SYNC_STACK_TEMPLATE_ID, version=INITIAL_TEMPLATE_VERSION),
        scenes=(
            Scene(
                order=0,
                duration_seconds=2.0,
                trim_start_seconds=trim_start,
                trim_duration_seconds=trim_duration,
                media=AssetRef(asset_id=asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
        metadata={"sync_stack.copies": 2},
    )


def test_sync_stack_rejects_trimmed_video_until_overlay_source_timebase_exists() -> None:
    asset = _video()
    for project in (
        _sync_project(asset, trim_start=1.0),
        _sync_project(asset, trim_duration=1.5),
    ):
        with pytest.raises(InitialTemplateError, match="does not support trimmed video"):
            compile_registered_template(project, {asset.asset_id: asset})


def test_sync_stack_rejects_unknown_or_short_video_duration() -> None:
    unknown = _video(duration=None)
    with pytest.raises(InitialTemplateError, match="duration metadata is required"):
        compile_registered_template(_sync_project(unknown), {unknown.asset_id: unknown})

    short = _video(duration=1.0, sha="b")
    with pytest.raises(InitialTemplateError, match="shorter than the synchronized scene"):
        compile_registered_template(_sync_project(short), {short.asset_id: short})


def test_sync_stack_binds_duplicate_overlay_duration_to_scene() -> None:
    asset = _video(duration=5.0)
    plan = compile_registered_template(_sync_project(asset), {asset.asset_id: asset})

    asset_overlays = [overlay for overlay in plan.overlays if overlay.asset_id == asset.asset_id]
    assert len(asset_overlays) == 1
    assert asset_overlays[0].start_seconds == 0.0
    assert asset_overlays[0].duration_seconds == 2.0


def _reaction_project(main: Asset, reaction: Asset) -> Project:
    return Project(
        content_kind="reaction",
        template=TemplateRef(
            template_id=REACTION_BOTTOM_TEMPLATE_ID,
            version=INITIAL_TEMPLATE_VERSION,
        ),
        scenes=(
            Scene(
                order=0,
                duration_seconds=2.0,
                media=AssetRef(asset_id=main.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(),),
        metadata={"reaction_bottom.reaction_asset_id": reaction.asset_id},
    )


def test_reaction_bottom_rejects_unknown_or_short_video_duration() -> None:
    main = _video(duration=5.0, sha="a")
    unknown = _video(duration=None, sha="b")
    with pytest.raises(InitialTemplateError, match="video duration metadata is required"):
        compile_registered_template(
            _reaction_project(main, unknown),
            {main.asset_id: main, unknown.asset_id: unknown},
        )

    short = _video(duration=1.0, sha="c")
    with pytest.raises(InitialTemplateError, match="shorter than the primary scene"):
        compile_registered_template(
            _reaction_project(main, short),
            {main.asset_id: main, short.asset_id: short},
        )


def test_reaction_bottom_binds_video_overlay_duration_to_primary_scene() -> None:
    main = _video(duration=5.0, sha="a")
    reaction = _video(duration=3.0, sha="b")
    plan = compile_registered_template(
        _reaction_project(main, reaction),
        {main.asset_id: main, reaction.asset_id: reaction},
    )

    assert len(plan.overlays) == 1
    assert plan.overlays[0].asset_id == reaction.asset_id
    assert plan.overlays[0].start_seconds == 0.0
    assert plan.overlays[0].duration_seconds == 2.0
