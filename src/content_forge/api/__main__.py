"""CLI entry point for the local Content Forge API server."""

from __future__ import annotations

import argparse
import ipaddress
from typing import Literal

import uvicorn

from content_forge.providers.tts import TTSProvider


def _is_loopback_bind_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_transport(
    host: str,
    *,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> None:
    if (ssl_certfile is None) != (ssl_keyfile is None):
        raise ValueError("TLS requires both --ssl-certfile and --ssl-keyfile")
    if not _is_loopback_bind_host(host) and (
        ssl_certfile is None or ssl_keyfile is None
    ):
        raise ValueError(
            "non-loopback/LAN binding requires TLS certificate and private key"
        )


def build_tts_provider(name: Literal["none", "qwen"]) -> TTSProvider | None:
    """Build an explicitly selected optional TTS runtime without eager model loading."""

    if name == "none":
        return None
    if name == "qwen":
        # Keep the heavy optional dependency behind explicit CLI selection. Constructing
        # the adapter is cheap and does not load/download model weights; PR20 health and
        # synthesis retain authority over package/config/model availability.
        from content_forge.providers.qwen_tts import QwenTTSProvider

        return QwenTTSProvider()
    raise ValueError(f"unsupported TTS provider: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Content Forge local API")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="bind all local interfaces; requires TLS certificate/key",
    )
    parser.add_argument("--host", default=None, help="explicit bind host override")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ssl-certfile", default=None)
    parser.add_argument("--ssl-keyfile", default=None)
    parser.add_argument(
        "--tts-provider",
        choices=("none", "qwen"),
        default="none",
        help=(
            "optional Voice Cast preview TTS runtime; qwen uses the pinned PR20 "
            "Qwen3-TTS adapter and requires the tts extra"
        ),
    )
    args = parser.parse_args()
    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    try:
        validate_transport(
            host,
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
        )
        tts_provider = build_tts_provider(args.tts_provider)
    except ValueError as exc:
        parser.error(str(exc))

    # Instantiate the app so the explicitly selected optional provider can be injected.
    # The default remains provider-free; selecting Qwen still does not load model weights
    # until PR20 synthesis actually needs the runtime.
    from content_forge.api import create_app

    app = create_app(tts_provider=tts_provider)
    uvicorn.run(
        app,
        host=host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


if __name__ == "__main__":
    main()
