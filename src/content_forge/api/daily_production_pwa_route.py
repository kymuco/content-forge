"""PR35 grouped attention projection composed over the proven Production Home controller."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response

from content_forge.web import static_path
from content_forge.web.routes import _harden, _production_home_script

_REFRESH_MARKER = "      renderSummary(buckets);\n      if (!entries.length) {"
_NAVIGATION_MARKER = "  watchAdvancedPanels();\n"


def _replace_once(source: str, old: str, new: str, *, marker: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"PR35 Production Home composition marker changed: {marker}")
    return source.replace(old, new, 1)


def _daily_production_home_script() -> str:
    """Add navigation hooks and grouped queue refresh without duplicating PR33 authority."""

    source = _production_home_script()
    source = _replace_once(
        source,
        _REFRESH_MARKER,
        "      renderSummary(buckets);\n"
        "      window.dispatchEvent(new CustomEvent(\"content-forge:production-home-refreshed\"));\n"
        "      if (!entries.length) {",
        marker="home refresh completion",
    )
    source = _replace_once(
        source,
        _NAVIGATION_MARKER,
        "  window.CFProductionHome = Object.freeze({\n"
        "    openProject,\n"
        "    openCreateVideo: openCreateWizard,\n"
        "    refreshHome,\n"
        "  });\n\n"
        + _NAVIGATION_MARKER,
        marker="project navigation export",
    )
    attention = static_path("attention-queue.js").read_text(encoding="utf-8")
    return source + "\n" + attention


def install_daily_production_pwa_route(app: FastAPI) -> None:
    """Replace exactly one packaged Production Home transport with the PR35 composition."""

    matches = [
        route
        for route in app.router.routes
        if getattr(route, "path", None) == "/app/production-home.js"
        and "GET" in (getattr(route, "methods", None) or set())
    ]
    if len(matches) != 1:
        raise RuntimeError("PR35 expected exactly one Production Home script route")
    app.router.routes.remove(matches[0])

    @app.get("/app/production-home.js", include_in_schema=False)
    def daily_production_home_script() -> Response:
        response = Response(
            _daily_production_home_script(),
            media_type="text/javascript; charset=utf-8",
        )
        return _harden(response, cache_control="no-cache")


__all__ = ["install_daily_production_pwa_route"]
