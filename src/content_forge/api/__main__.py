"""CLI entry point for the local Content Forge API server."""

from __future__ import annotations

import argparse
import ipaddress

import uvicorn


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
    args = parser.parse_args()
    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    try:
        validate_transport(
            host,
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
        )
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(
        "content_forge.api:create_app",
        factory=True,
        host=host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


if __name__ == "__main__":
    main()
