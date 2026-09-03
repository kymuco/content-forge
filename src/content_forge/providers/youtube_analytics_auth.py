"""Local OAuth bootstrap for the PR37 read-only YouTube Analytics adapter."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .youtube_analytics import YOUTUBE_ANALYTICS_OAUTH_SCOPES
from .youtube_auth import write_private_token


def _token_target(path: Path) -> Path:
    """Resolve parent aliases without following a final-component token symlink."""

    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    parent = expanded.parent.resolve()
    target = parent / expanded.name
    if target.is_symlink():
        raise RuntimeError("YouTube Analytics OAuth token path must not be a symlink")
    if target.exists() and not target.is_file():
        raise RuntimeError("YouTube Analytics OAuth token path must identify a regular file")
    return target


def _authorized_channel_id(credentials: object) -> str:
    try:
        from googleapiclient.discovery import build
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("google-api-python-client is required for YouTube authorization") from exc
    try:
        service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        payload: Any = service.channels().list(
            part="id",
            mine=True,
            maxResults=2,
        ).execute(num_retries=3)
    except Exception as exc:  # pragma: no cover - network/account environment
        raise RuntimeError("authorized YouTube channel lookup failed") from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError("authorization did not resolve exactly one YouTube channel")
    channel_id = items[0].get("id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise RuntimeError("authorized YouTube channel ID is missing")
    return channel_id.strip()


def authorize_youtube_analytics(
    *,
    client_secrets_path: Path,
    token_path: Path,
    open_browser: bool = True,
) -> str:
    """Authorize a separate read-only analytics token and return its exact channel ID."""

    client_secrets_path = client_secrets_path.expanduser().resolve()
    token_path = _token_target(token_path)
    if not client_secrets_path.is_file():
        raise RuntimeError("YouTube Analytics OAuth client-secrets file is missing")
    if client_secrets_path == token_path:
        raise RuntimeError("YouTube Analytics client-secrets and token paths must be different")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("google-auth-oauthlib is required for YouTube authorization") from exc

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path),
            scopes=list(YOUTUBE_ANALYTICS_OAUTH_SCOPES),
        )
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=open_browser,
            access_type="offline",
            prompt="consent",
        )
    except Exception as exc:  # pragma: no cover - browser/Google environment
        raise RuntimeError("YouTube Analytics OAuth authorization failed") from exc

    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        raise RuntimeError(
            "YouTube Analytics OAuth authorization did not return a refresh token; "
            "revoke the grant and retry"
        )
    has_scopes = getattr(credentials, "has_scopes", None)
    if callable(has_scopes) and not has_scopes(YOUTUBE_ANALYTICS_OAUTH_SCOPES):
        raise RuntimeError("YouTube Analytics OAuth authorization did not grant required scopes")

    channel_id = _authorized_channel_id(credentials)
    try:
        payload = credentials.to_json()
    except Exception as exc:
        raise RuntimeError("YouTube Analytics OAuth token could not be serialized") from exc
    write_private_token(token_path, payload)
    return channel_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize Content Forge for read-only YouTube Analytics collection"
    )
    parser.add_argument(
        "--client-secrets",
        required=True,
        help="Google OAuth desktop-app client secrets JSON path",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="private local path for the separate analytics authorized-user token JSON",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not automatically open the browser during local OAuth consent",
    )
    args = parser.parse_args()
    try:
        channel_id = authorize_youtube_analytics(
            client_secrets_path=Path(args.client_secrets),
            token_path=Path(args.token),
            open_browser=not args.no_browser,
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"YouTube Analytics authorization stored. channel_id={channel_id}")


if __name__ == "__main__":
    main()


__all__ = ["authorize_youtube_analytics", "main"]
