"""Local HTTP transport for Content Forge."""

from __future__ import annotations

from pathlib import Path

from content_forge.providers.tts import TTSProvider
from content_forge.web.routes import install_pwa_routes

from .app import (
    MULTIPART_OVERHEAD_BUDGET,
    _pairing_bootstrap_allowed,
    create_app as _create_api_app,
)
from .dialogue_routes import install_dialogue_routes
from .review_prepare_routes import install_review_prepare_route
from .review_routes import install_review_routes
from .voice_cast_routes import install_voice_cast_routes
from .voiced_story_routes import install_voiced_story_routes


def create_app(
    *,
    root: str | Path | None = None,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024,
    tts_provider: TTSProvider | None = None,
):
    """Build the local API/PWA with PR21 voice and PR22 voiced-story surfaces."""

    app = _create_api_app(
        root=root,
        ffprobe_path=ffprobe_path,
        ffmpeg_path=ffmpeg_path,
        max_upload_bytes=max_upload_bytes,
    )
    try:
        review = install_review_routes(
            app,
            auth=app.state.auth,
            library=app.state.library,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
        install_review_prepare_route(
            app,
            auth=app.state.auth,
            review=review,
        )
        install_dialogue_routes(
            app,
            auth=app.state.auth,
            library=app.state.library,
        )
        install_voice_cast_routes(
            app,
            auth=app.state.auth,
            library=app.state.library,
            tts_provider=tts_provider,
        )
        install_voiced_story_routes(
            app,
            auth=app.state.auth,
            library=app.state.library,
            tts_provider=tts_provider,
        )
        # The PR8 RuntimeLease is already held exclusively by _create_api_app(). This is
        # therefore the safe crash-recovery point for PR10 render/preview claims: no old
        # process can still be executing jobs in this runtime root while reconciliation
        # adopts succeeded receipts or retires orphaned running states.
        review.reconcile_persisted_state()
        install_pwa_routes(
            app,
            auth=app.state.auth,
            pairing_bootstrap_allowed=_pairing_bootstrap_allowed,
            max_upload_bytes=max_upload_bytes,
            share_body_limit=max_upload_bytes + MULTIPART_OVERHEAD_BUDGET,
        )
    except BaseException:
        app.state.runtime_lease.close()
        raise
    return app


__all__ = ["create_app"]
