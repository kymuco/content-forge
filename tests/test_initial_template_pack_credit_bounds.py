from __future__ import annotations

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    Project,
    Scene,
    SourceRecord,
    TemplateRef,
    new_entity_id,
)
from content_forge.profiles import shorts_preview_profile
from content_forge.templates import (
    ART_STORY_TEMPLATE_ID,
    INITIAL_TEMPLATE_VERSION,
    InitialTemplateError,
    compile_registered_template,
)


def _image(*, sha: str) -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256=sha * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=100,
        width=720,
        height=1280,
        has_audio=False,
    )


def _project(
    used: Asset,
    records: tuple[SourceRecord, ...],
) -> Project:
    return Project(
        content_kind="art",
        template=TemplateRef(
            template_id=ART_STORY_TEMPLATE_ID,
            version=INITIAL_TEMPLATE_VERSION,
        ),
        scenes=(
            Scene(
                order=0,
                duration_seconds=2.0,
                media=AssetRef(asset_id=used.asset_id),
            ),
        ),
        source_refs=tuple(
            AssetRef(asset_id=record.asset_id, source_id=record.source_id)
            for record in records
        ),
        source_records=records,
        output_profiles=(shorts_preview_profile(),),
    )


def test_art_story_fails_closed_when_used_source_requires_missing_credit() -> None:
    used = _image(sha="a")
    required = SourceRecord(
        asset_id=used.asset_id,
        requires_credit=True,
        credit_text=None,
    )
    project = _project(used, (required,))

    with pytest.raises(InitialTemplateError, match="requires non-empty credit_text"):
        compile_registered_template(project, {used.asset_id: used})


def test_art_story_ignores_credit_records_for_unused_assets() -> None:
    used = _image(sha="a")
    unused = _image(sha="b")
    used_record = SourceRecord(
        asset_id=used.asset_id,
        requires_credit=True,
        credit_text="Artist: used",
    )
    unused_record = SourceRecord(
        asset_id=unused.asset_id,
        credit_text="Artist: unrelated",
    )
    project = _project(used, (used_record, unused_record))

    plan = compile_registered_template(
        project,
        {used.asset_id: used, unused.asset_id: unused},
    )

    assert len(plan.overlays) == 1
    assert plan.overlays[0].text == "Artist: used"


def test_art_story_accepts_used_required_credit_and_renders_it() -> None:
    used = _image(sha="a")
    record = SourceRecord(
        asset_id=used.asset_id,
        requires_credit=True,
        credit_text="Artist: fixture",
    )
    project = _project(used, (record,))

    plan = compile_registered_template(project, {used.asset_id: used})

    assert plan.overlays[0].text == "Artist: fixture"
