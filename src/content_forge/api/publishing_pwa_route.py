"""Packaged PR27/PR34 publishing PWA module route."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response

from content_forge.web import static_path


def install_publishing_pwa_route(app: FastAPI) -> None:
    @app.get("/app/publishing.js", include_in_schema=False)
    def publishing_script() -> Response:
        # Keep the proven Advanced PR27/PR29 module intact and compose the PR34
        # project-specific phone projection into the same cached script URL. Existing
        # HTML and CSP therefore need no new script surface.
        content = (
            static_path("publishing.js").read_text(encoding="utf-8")
            + "\n"
            + static_path("project-publishing.js").read_text(encoding="utf-8")
        )
        response = Response(content, media_type="text/javascript; charset=utf-8")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


__all__ = ["install_publishing_pwa_route"]
