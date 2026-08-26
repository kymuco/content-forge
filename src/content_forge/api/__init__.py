"""Local HTTP transport for Content Forge."""

from __future__ import annotations

from pathlib import Path

from content_forge.web.routes import install_pwa_routes

from .app import (
    MULTIPART_OVERHEAD_BUDGET,
    _pairing_bootstrap_allowed,
    create_app as _create_api_app,
)


def create_app(
    *,
    root: str | Path | None = None,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024,
):
    """Build the PR8 API plus the PR9 packaged PWA transport surface."""

    app = _create_api_app(
        root=root,
        ffprobe_path=ffprobe_path,
        ffmpeg_path=ffmpeg_path,
        max_upload_bytes=max_upload_bytes,
    )
    try:
        install_pwa_routes(
            app,
            auth=app.state.auth,
            pairing_bootstrap_allowed=_pairing_bootstrap_allowed,
            share_body_limit=max_upload_bytes + MULTIPART_OVERHEAD_BUDGET,
        )
    except BaseException:
        app.state.runtime_lease.close()
        raise
    return app


__all__ = ["create_app"]
