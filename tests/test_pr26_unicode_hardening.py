from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from content_forge.storage import LibrarySearchQuery, LibraryTag, LocalLibrary


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def test_pr26_prefix_search_includes_non_bmp_suffixes(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ingest = library.assets.ingest_file(_write(tmp_path / "emoji.png", b"emoji-prefix"))
    library.index.set_tags(
        ingest.asset.asset_id,
        (LibraryTag(kind="topic", value="Hero😀Archive"),),
    )

    hits = library.index.search(LibrarySearchQuery(tag_prefix="HERO"))
    assert [hit.asset.asset_id for hit in hits] == [ingest.asset.asset_id]


def test_pr26_prefix_search_handles_max_unicode_scalar_suffix(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ingest = library.assets.ingest_file(_write(tmp_path / "max.png", b"max-prefix"))
    marker = chr(0x10FFFF)
    library.index.set_tags(
        ingest.asset.asset_id,
        (LibraryTag(kind="topic", value=f"A{marker}tail"),),
    )

    hits = library.index.search(LibrarySearchQuery(tag_prefix=f"a{marker}"))
    assert [hit.asset.asset_id for hit in hits] == [ingest.asset.asset_id]


def test_pr26_tag_contract_rejects_lone_surrogates_before_sqlite() -> None:
    # Pydantic may reject a lone surrogate while decoding its constrained string before
    # PR26's field validator runs. Either path is correct: malformed non-scalar text must
    # fail validation and never reach sqlite3's UTF-8 encoder.
    with pytest.raises(ValidationError):
        LibraryTag(kind="topic", value="\ud800")
    with pytest.raises(ValidationError):
        LibrarySearchQuery(tag_prefix="\udfff")
