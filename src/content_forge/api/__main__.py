"""CLI entry point for the local Content Forge API server."""

from __future__ import annotations

import argparse
import ipaddress
from typing import Literal

import uvicorn

from content_forge.providers.publishing import PublishingProvider
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
        from content_forge.providers.qwen_tts import QwenTTSProvider

        return QwenTTSProvider()
    raise ValueError(f"unsupported TTS provider: {name}")


def build_publishing_provider(
    name: Literal["none", "youtube"],
    *,
    youtube_token_path: str | None,
    youtube_channel_id: str | None,
    youtube_category_id: str | None = None,
) -> PublishingProvider | None:
    """Build one explicitly selected remote publisher without loading OAuth secrets eagerly."""

    if name == "none":
        if (
            youtube_token_path is not None
            or youtube_channel_id is not None
            or youtube_category_id is not None
        ):
            raise ValueError(
                "YouTube runtime options require --publishing-provider youtube"
            )
        return None
    if name == "youtube":
        if not youtube_token_path:
            raise ValueError("--youtube-token is required for YouTube publishing")
        if not youtube_channel_id:
            raise ValueError("--youtube-channel-id is required for YouTube publishing")
        from content_forge.providers.youtube import (
            YouTubePublishingConfig,
            YouTubePublishingProvider,
        )

        return YouTubePublishingProvider(
            YouTubePublishingConfig(
                token_path=youtube_token_path,
                channel_id=youtube_channel_id,
                category_id=youtube_category_id or "22",
            )
        )
    raise ValueError(f"unsupported publishing provider: {name}")


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
    parser.add_argument(
        "--publishing-provider",
        choices=("none", "youtube"),
        default="none",
        help=(
            "optional PR28 remote publishing runtime; youtube requires the youtube extra "
            "plus an explicitly authorized local token and channel ID"
        ),
    )
    parser.add_argument(
        "--youtube-token",
        default=None,
        help="owner-only authorized-user OAuth token JSON created by content-forge-youtube-auth",
    )
    parser.add_argument(
        "--youtube-channel-id",
        default=None,
        help="exact YouTube channel ID bound to the authorized publishing runtime",
    )
    parser.add_argument(
        "--youtube-category-id",
        default=None,
        help=(
            "assignable YouTube video category ID; defaults to 22 (People & Blogs) "
            "when the YouTube publisher is selected"
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
        publishing_provider = build_publishing_provider(
            args.publishing_provider,
            youtube_token_path=args.youtube_token,
            youtube_channel_id=args.youtube_channel_id,
            youtube_category_id=args.youtube_category_id,
        )
    except ValueError as exc:
        parser.error(str(exc))

    from content_forge.api import create_app

    app = create_app(
        tts_provider=tts_provider,
        publishing_provider=publishing_provider,
    )
    uvicorn.run(
        app,
        host=host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


if __name__ == "__main__":
    main()
