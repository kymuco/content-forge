from __future__ import annotations

import pytest

from content_forge.api.__main__ import build_tts_provider, validate_transport
from content_forge.providers.qwen_tts import QwenTTSProvider


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
