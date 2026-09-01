from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from content_forge.core import AssetRef, Project
from content_forge.storage import (
    LibrarySearchQuery,
    LibraryTag,
    LibraryTagKind,
    LocalLibrary,
    ProductionLibraryIndex,
    SourceInput,
    StorageSchemaError,
)


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def test_pr26_tags_normalize_and_search_with_and_semantics(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    raiden = library.assets.ingest_file(_write(tmp_path / "raiden.png", b"raiden"))
    nahida = library.assets.ingest_file(_write(tmp_path / "nahida.png", b"nahida"))

    raiden_tags = library.index.set_tags(
        raiden.asset.asset_id,
        (
            LibraryTag(kind="game", value="  Ｇenshin   Impact "),
            LibraryTag(kind="character", value="Raiden Shogun"),
            LibraryTag(kind="topic", value="Build Guide"),
        ),
    )
    library.index.set_tags(
        nahida.asset.asset_id,
        (
            LibraryTag(kind=LibraryTagKind.GAME, value="Genshin Impact"),
            LibraryTag(kind=LibraryTagKind.CHARACTER, value="Nahida"),
        ),
    )

    assert raiden_tags[1].value == "Genshin Impact"
    assert {tag.value_key for tag in raiden_tags} == {
        "genshin impact",
        "raiden shogun",
        "build guide",
    }

    hits = library.index.search(
        LibrarySearchQuery(
            tags=(
                LibraryTag(kind="game", value="GENSHIN IMPACT"),
                LibraryTag(kind="character", value="raiden shogun"),
            )
        )
    )
    assert [hit.asset.asset_id for hit in hits] == [raiden.asset.asset_id]
    assert hits[0].previously_used is False

    prefix_hits = library.index.search(LibrarySearchQuery(tag_prefix="rai"))
    assert [hit.asset.asset_id for hit in prefix_hits] == [raiden.asset.asset_id]


def test_pr26_tag_contract_rejects_empty_control_and_duplicate_query_tags() -> None:
    with pytest.raises(ValidationError):
        LibraryTag(kind="topic", value="   ")
    with pytest.raises(ValidationError):
        LibraryTag(kind="topic", value="safe\u200bhidden")
    with pytest.raises(ValidationError):
        LibrarySearchQuery(
            tags=(
                LibraryTag(kind="topic", value="News"),
                LibraryTag(kind="topic", value="news"),
            )
        )


def test_pr26_duplicate_warning_reuses_existing_sha_authority(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    first = library.assets.ingest_file(
        _write(tmp_path / "one.png", b"same-bytes"),
        source=SourceInput(source_url="https://example.invalid/one", platform="source_a"),
    )
    second = library.assets.ingest_file(
        _write(tmp_path / "two.png", b"same-bytes"),
        source=SourceInput(source_url="https://example.invalid/two", platform="source_b"),
    )
    assert second.deduplicated is True
    assert first.asset.asset_id == second.asset.asset_id

    info = library.index.duplicate_info(first.asset.sha256.upper())
    assert info is not None
    assert info.asset.asset_id == first.asset.asset_id
    assert info.source_count == 2
    assert info.has_multiple_sources is True
    assert info.project_count == 0

    assert library.index.duplicate_info("0" * 64) is None
    with pytest.raises(ValueError, match="SHA-256"):
        library.index.duplicate_info("not-a-digest")


def test_pr26_previously_used_and_reuse_history_derive_from_project_assets(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ingest = library.assets.ingest_file(
        _write(tmp_path / "panel.png", b"panel"),
        source=SourceInput(
            source_url="https://example.invalid/artist/post",
            platform="artist_site",
            creator_handle="@artist",
        ),
    )
    assert ingest.source_record is not None
    library.index.set_tags(
        ingest.asset.asset_id,
        (LibraryTag(kind="artist", value="Example Artist"),),
    )

    unused = library.index.search(LibrarySearchQuery(previously_used=False))
    assert [hit.asset.asset_id for hit in unused] == [ingest.asset.asset_id]

    ref = AssetRef(
        asset_id=ingest.asset.asset_id,
        source_id=ingest.source_record.source_id,
        role="panel",
    )
    first = Project(
        content_kind="art_story",
        source_refs=(ref,),
        source_records=(ingest.source_record,),
    )
    second = Project(
        content_kind="long_form",
        source_refs=(ref,),
        source_records=(ingest.source_record,),
    )
    library.save_project(first)
    library.save_project(second)

    used = library.index.search(LibrarySearchQuery(previously_used=True))
    assert [hit.asset.asset_id for hit in used] == [ingest.asset.asset_id]
    assert used[0].project_count == 2

    history = library.index.reuse_history(ingest.asset.asset_id)
    assert {item.project_id for item in history} == {first.project_id, second.project_id}
    assert {item.role for item in history} == {"panel"}
    assert {item.source_id for item in history} == {ingest.source_record.source_id}

    info = library.index.duplicate_info(ingest.asset.sha256)
    assert info is not None and info.previously_used is True and info.project_count == 2


def test_pr26_virtual_collection_is_saved_query_not_physical_membership(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    first = library.assets.ingest_file(_write(tmp_path / "first.png", b"first"))
    second = library.assets.ingest_file(_write(tmp_path / "second.png", b"second"))
    target = LibraryTag(kind="anime", value="Frieren")
    library.index.set_tags(first.asset.asset_id, (target,))

    saved = library.index.put_collection(
        "frieren_panels",
        "  Frieren   panels ",
        LibrarySearchQuery(tags=(target,)),
    )
    assert saved.name == "Frieren panels"
    assert library.index.get_collection("frieren_panels") == saved
    assert library.index.list_collections() == (saved,)
    assert [hit.asset.asset_id for hit in library.index.search_collection("frieren_panels")] == [
        first.asset.asset_id
    ]

    library.index.set_tags(first.asset.asset_id, ())
    library.index.set_tags(second.asset.asset_id, (target,))
    assert [hit.asset.asset_id for hit in library.index.search_collection("frieren_panels")] == [
        second.asset.asset_id
    ]
    assert library.index.delete_collection("frieren_panels") is True
    assert library.index.delete_collection("frieren_panels") is False


def test_pr26_extension_initializes_over_existing_library_without_mutating_assets(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ingest = library.assets.ingest_file(_write(tmp_path / "existing.png", b"existing"))
    original = library.database.get_asset(ingest.asset.asset_id)

    # Reinitialization is idempotent and the extension uses its own application-schema marker.
    ProductionLibraryIndex(library.database).initialize()
    assert library.database.get_asset(ingest.asset.asset_id) == original
    with library.database.connection() as connection:
        version = connection.execute(
            "SELECT version FROM application_schema WHERE component = 'production_library'"
        ).fetchone()
        assert version is not None and int(version["version"]) == 1
        assert connection.execute("SELECT COUNT(*) FROM library_asset_tags").fetchone()[0] == 0


def test_pr26_extension_fails_closed_on_future_feature_schema(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    _ = library.index
    with library.database.transaction() as connection:
        connection.execute(
            "UPDATE application_schema SET version = 2 WHERE component = 'production_library'"
        )
    with pytest.raises(StorageSchemaError, match="newer than supported"):
        ProductionLibraryIndex(library.database).initialize()
