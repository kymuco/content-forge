from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.providers.youtube import _media_upload


def test_pr28_real_google_media_upload_factory_is_resumable_mp4(tmp_path: Path) -> None:
    pytest.importorskip("googleapiclient.http")
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"test")

    with media.open("rb") as handle:
        upload = _media_upload(handle)
        assert upload.resumable() is True
        assert upload.mimetype() == "video/mp4"
        assert upload.size() == 4
