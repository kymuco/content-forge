from __future__ import annotations

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from content_forge.api import create_app


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real FFmpeg integration requires ffmpeg and ffprobe",
)


def test_authenticated_upload_prepares_real_video_and_thumbnail(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x96:r=10:d=0.6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )

    runtime = tmp_path / "runtime"
    client = TestClient(create_app(root=runtime))
    challenge = client.post(
        "/api/v1/pairing/challenges",
        headers={"Host": "localhost"},
    ).json()
    exchange = client.post(
        "/api/v1/pairing/exchange",
        json={"challenge_id": challenge["challenge_id"], "code": challenge["code"]},
    )
    token = exchange.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    with source.open("rb") as handle:
        response = client.post(
            "/api/v1/inbox/files",
            headers=headers,
            files={"file": ("misleading.txt", handle, "text/plain")},
            data={"note": "real ffmpeg boundary"},
        )
    assert response.status_code == 201, response.text
    intake = response.json()
    assert intake["state"] == "prepared"
    assert intake["probe_state"] == "succeeded"
    assert intake["thumbnail_state"] == "succeeded"

    asset = client.app.state.library.database.get_asset(intake["asset_id"])
    assert asset is not None
    assert asset.media_type.value == "video"
    assert asset.mime_type == "video/mp4"

    thumbnail = client.get(
        f"/api/v1/assets/{intake['asset_id']}/thumbnail",
        headers=headers,
    )
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/jpeg")
    assert len(thumbnail.content) > 0
