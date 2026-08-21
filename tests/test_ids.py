from content_forge.core import EntityKind, new_entity_id, require_entity_id


def test_entity_ids_are_prefixed_and_unique() -> None:
    first = new_entity_id(EntityKind.PROJECT)
    second = new_entity_id(EntityKind.PROJECT)

    assert first.startswith("cf_project_")
    assert len(first) == len("cf_project_") + 32
    assert first != second
    assert require_entity_id(first, EntityKind.PROJECT) == first


def test_entity_id_rejects_wrong_kind() -> None:
    asset_id = new_entity_id(EntityKind.ASSET)

    try:
        require_entity_id(asset_id, EntityKind.PROJECT)
    except ValueError as exc:
        assert "expected project ID" in str(exc)
    else:
        raise AssertionError("wrong entity kind should fail validation")
