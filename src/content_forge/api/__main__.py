"""CLI entry point for the local Content Forge API server."""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Content Forge local API")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="explicitly bind all local interfaces for phone access",
    )
    parser.add_argument("--host", default=None, help="explicit bind host override")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    uvicorn.run("content_forge.api:create_app", factory=True, host=host, port=args.port)


if __name__ == "__main__":
    main()
