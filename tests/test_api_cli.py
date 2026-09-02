from __future__ import annotations

import pytest

from content_forge.api.__main__ import (
    build_publishing_provider,
    build_tts_provider,
    validate_transport,
)
from content_forge.providers.qwen_tts import QwenTTSProvider
from content_forge.providers.youtube import YouTubePublishingProvider


def test_plain_http_is_loopback_only() -> None:
    validate_transport("127.0.0.1", ssl_certfile=None, ssl_keyfile=None)
    validate_transport("localhost", ssl_certfile=None, ssl_keyfile=None)

    with pytest.raises(ValueError, match="requires TLS"):
        validate_transport("0.0.0.0", ssl_certfile=None, ssl_keyfile=None)
    with pytest.raises(ValueError, match="requires TLS"):
        validate_transport("192.168.1.50", ssl_certfile=None, ssl_keyfile=None)

    validate_transport(
        "0.0.0.0",
        ssl_certfile="content-forge.crt",
        ssl_keyfile="content-forge.key",
    )


def test_tls_configuration_requires_cert_and_key_pair() -> None:
    with pytest.raises(ValueError, match="both"):
        validate_transport(
            "127.0.0.1",
            ssl_certfile="content-forge.crt",
            ssl_keyfile=None,
        )


def test_cli_tts_selection_is_explicit_and_qwen_stays_lazy() -> None:
    assert build_tts_provider("none") is None
    provider = build_tts_provider("qwen")
    assert isinstance(provider, QwenTTSProvider)
    assert provider._runtime is None


def test_cli_youtube_publishing_selection_is_explicit_and_credentials_stay_lazy() -> None:
    assert build_publishing_provider(
        "none",
        youtube_token_path=None,
        youtube_channel_id=None,
        youtube_category_id=None,
    ) is None

    with pytest.raises(ValueError, match="--youtube-token"):
        build_publishing_provider(
            "youtube",
            youtube_token_path=None,
            youtube_channel_id="UC123",
            youtube_category_id=None,
        )
    with pytest.raises(ValueError, match="--youtube-channel-id"):
        build_publishing_provider(
            "youtube",
            youtube_token_path="/local/youtube-token.json",
            youtube_channel_id=None,
            youtube_category_id=None,
        )
    with pytest.raises(ValueError, match="require --publishing-provider youtube"):
        build_publishing_provider(
            "none",
            youtube_token_path="/local/youtube-token.json",
            youtube_channel_id="UC123",
            youtube_category_id="24",
        )

    provider = build_publishing_provider(
        "youtube",
        youtube_token_path="/local/youtube-token.json",
        youtube_channel_id="UC123",
        youtube_category_id=None,
    )
    assert isinstance(provider, YouTubePublishingProvider)
    assert provider.config.token_path == "/local/youtube-token.json"
    assert provider.config.channel_id == "UC123"
    assert provider.config.category_id == "22"

    explicit = build_publishing_provider(
        "youtube",
        youtube_token_path="/local/youtube-token.json",
        youtube_channel_id="UC123",
        youtube_category_id="24",
    )
    assert isinstance(explicit, YouTubePublishingProvider)
    assert explicit.config.category_id == "24"
    assert explicit._version.endswith(":category=24:notify=0")
