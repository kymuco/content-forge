"""Packaged PR27 publishing PWA module route."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse

from content_forge.web import static_path


def install_publishing_pwa_route(app: FastAPI) -> None:
    @app.get("/app/publishing.js", include_in_schema=False)
    def publishing_script() -> FileResponse:
        response = FileResponse(
            static_path("publishing.js"),
            media_type="text/javascript; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


__all__ = ["install_publishing_pwa_route"]
