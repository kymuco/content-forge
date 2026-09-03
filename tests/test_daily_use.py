from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from content_forge.api import create_app
from content_forge.daily import (
    DailyUseProfile,
    canonicalize_profile,
    doctor_profile,
    load_profile,
    profile_path,
    profile_ready,
    save_profile,
)


def _local_file(path: Path, content: str, *, private: bool = False) -> Path:
    path.write_text(content, encoding="utf-8")
    if private and os.name != "nt":
        path.chmod(0o600)
    return path


def _profile(tmp_path: Path) -> DailyUseProfile:
    certificate = _local_file(tmp_path / "content-forge.crt", "test certificate")
    private_key = _local_file(
        tmp_path / "content-forge.key",
        "test private key",
        private=True,
    )
    return DailyUseProfile(
        public_base_url="https://forge.home.test:8765/",
        ssl_certfile=str(certificate),
        ssl_keyfile=str(private_key),
        ffmpeg_path=sys.executable,
        ffprobe_path=sys.executable,
    )


def test_daily_profile_round_trip_and_doctor(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    profile = _profile(tmp_path)

    target = save_profile(profile, root=runtime)
    assert target == profile_path(runtime)
    assert target.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    loaded = load_profile(root=runtime)
    assert loaded.public_base_url == "https://forge.home.test:8765"
    assert loaded.phone_app_url == "https://forge.home.test:8765/app/"
    assert loaded.ssl_certfile == str((tmp_path / "content-forge.crt").resolve())
    assert loaded.ssl_keyfile == str((tmp_path / "content-forge.key").resolve())

    checks = doctor_profile(loaded, root=runtime)
    assert profile_ready(checks)
    assert {check.name for check in checks} == {
        "runtime",
        "tls-certificate",
        "tls-private-key",
        "ffmpeg",
        "ffprobe",
        "phone-url",
    }


def test_daily_profile_rejects_plaintext_phone_url_and_partial_youtube_config() -> None:
    with pytest.raises(ValidationError, match="requires HTTPS"):
        DailyUseProfile(
            public_base_url="http://192.168.1.20:8765",
            ssl_certfile="certificate.pem",
            ssl_keyfile="private-key.pem",
        )

    with pytest.raises(ValidationError, match="youtube_channel_id"):
        DailyUseProfile(
            public_base_url="https://forge.home.test:8765",
            ssl_certfile="certificate.pem",
            ssl_keyfile="private-key.pem",
            publishing_provider="youtube",
            youtube_token_path="youtube-token.json",
        )


def test_daily_profile_rejects_private_file_symlink(tmp_path) -> None:
    if os.name == "nt":
        pytest.skip("creating symlinks requires additional privileges on some Windows hosts")

    certificate = _local_file(tmp_path / "certificate.pem", "certificate")
    private_key = _local_file(tmp_path / "private-key.pem", "private", private=True)
    alias = tmp_path / "private-key-alias.pem"
    alias.symlink_to(private_key)
    profile = DailyUseProfile(
        public_base_url="https://forge.home.test:8765",
        ssl_certfile=str(certificate),
        ssl_keyfile=str(alias),
        ffmpeg_path=sys.executable,
        ffprobe_path=sys.executable,
    )

    with pytest.raises(ValueError, match="symlink"):
        canonicalize_profile(profile)


def test_daily_phone_url_is_projected_into_pwa_config(tmp_path) -> None:
    app = create_app(
        root=tmp_path,
        public_base_url="https://forge.home.test:8765/",
    )
    client = TestClient(app)
    try:
        response = client.get("/app/config.json", headers={"Host": "localhost"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["publicBaseUrl"] == "https://forge.home.test:8765"

        script = client.get("/app/config.js", headers={"Host": "localhost"})
        assert script.status_code == 200
        prefix = "self.CF_CONFIG = Object.freeze("
        suffix = ");\n"
        encoded = script.text[len(prefix) : -len(suffix)]
        assert json.loads(encoded)["publicBaseUrl"] == "https://forge.home.test:8765"
    finally:
        app.state.runtime_lease.close()
