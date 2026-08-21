from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    Project,
    Scene,
    SourceRecord,
    new_entity_id,
)


def test_sha256_is_normalized_to_lowercase() -> None:
    asset = Asset(
        sha256="A" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=123,
    )
    assert asset.sha256 == "a" * 64


def test_source_record_can_preserve_unknown_permission_state() -> None:
    asset_id = new_entity_id(EntityKind.ASSET)
    record = SourceRecord(
        asset_id=asset_id,
        source_url="https://example.invalid/art",
        creator_handle="@artist",
        collected_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert record.permission_status.value == "unknown"
    assert record.credit_text is None


def test_source_record_must_reference_project_source_asset() -> None:
    project_id = new_entity_id(EntityKind.PROJECT)
    asset_id = new_entity_id(EntityKind.ASSET)
    other_asset_id = new_entity_id(EntityKind.ASSET)

    with pytest.raises(ValidationError, match="must appear in project source_refs"):
        Project(
            project_id=project_id,
            content_kind="art_story",
            source_refs=(AssetRef(asset_id=asset_id),),
            source_records=(SourceRecord(asset_id=other_asset_id),),
        )


def test_asset_ref_source_id_must_exist_in_project_provenance() -> None:
    asset_id = new_entity_id(EntityKind.ASSET)
    dangling_source_id = new_entity_id(EntityKind.SOURCE)

    with pytest.raises(
        ValidationError,
        match="source_id must identify a project source record",
    ):
        Project(
            content_kind="character_moment",
            source_refs=(
                AssetRef(asset_id=asset_id, source_id=dangling_source_id),
            ),
        )


def test_asset_ref_source_id_must_belong_to_same_asset() -> None:
    asset_a = new_entity_id(EntityKind.ASSET)
    asset_b = new_entity_id(EntityKind.ASSET)
    source_a = new_entity_id(EntityKind.SOURCE)
    source_b = new_entity_id(EntityKind.SOURCE)

    with pytest.raises(
        ValidationError,
        match="source_id must belong to the referenced asset_id",
    ):
        Project(
            content_kind="character_moment",
            source_refs=(
                AssetRef(asset_id=asset_a, source_id=source_b),
                AssetRef(asset_id=asset_b, source_id=source_a),
            ),
            source_records=(
                SourceRecord(source_id=source_a, asset_id=asset_a),
                SourceRecord(source_id=source_b, asset_id=asset_b),
            ),
        )


def test_nested_scene_asset_ref_also_validates_provenance_link() -> None:
    asset_id = new_entity_id(EntityKind.ASSET)
    dangling_source_id = new_entity_id(EntityKind.SOURCE)

    with pytest.raises(
        ValidationError,
        match="source_id must identify a project source record",
    ):
        Project(
            content_kind="character_moment",
            source_refs=(AssetRef(asset_id=asset_id),),
            scenes=(
                Scene(
                    order=0,
                    duration_seconds=1.0,
                    media=AssetRef(
                        asset_id=asset_id,
                        source_id=dangling_source_id,
                    ),
                ),
            ),
        )
