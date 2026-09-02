"""Packaged PR27/PR34 publishing PWA module route."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response

from content_forge.web import static_path


def _replace_once(source: str, old: str, new: str, *, marker: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"PR34 publishing bundle marker changed: {marker}")
    return source.replace(old, new, 1)


def _project_publishing_script() -> str:
    """Compose fail-closed exact-final guards over the packaged PR34 phone module."""

    source = static_path("project-publishing.js").read_text(encoding="utf-8")
    source = _replace_once(
        source,
        '''  function statePriority(state) {\n    if (state === "succeeded") return 5;\n    if (state === "outcome_unknown") return 4;\n    if (state === "running") return 3;\n    if (state === "prepared") return 2;\n    if (state === "failed") return 1;\n    return 0;\n  }''',
        '''  function statePriority(state) {\n    if (state === "outcome_unknown") return 5;\n    if (state === "running") return 4;\n    if (state === "succeeded") return 3;\n    if (state === "prepared") return 2;\n    if (state === "failed") return 1;\n    return 0;\n  }''',
        marker="dangerous publish state priority",
    )
    source = _replace_once(
        source,
        '''        `publishing/projects/${encodeURIComponent(project.project_id)}?limit=50`''',
        '''        `publishing/projects/${encodeURIComponent(project.project_id)}`\n          + `?render_job_id=${encodeURIComponent(project.final.job_id)}`\n          + `&output_sha256=${encodeURIComponent(project.final.output_sha256)}`\n          + "&limit=50"''',
        marker="exact final publishing projection query",
    )
    source = _replace_once(
        source,
        '''      if (context.project_id !== project.project_id) {\n        throw new Error("Publishing context returned a different project identity.");\n      }''',
        '''      if (context.project_id !== project.project_id\n          || context.render_job_id !== project.final.job_id\n          || context.output_sha256 !== project.final.output_sha256) {\n        throw new Error("Publishing context returned a different final identity.");\n      }''',
        marker="exact final publishing projection response",
    )
    return source


def install_publishing_pwa_route(app: FastAPI) -> None:
    @app.get("/app/publishing.js", include_in_schema=False)
    def publishing_script() -> Response:
        # Keep the proven Advanced PR27/PR29 module intact and compose the PR34
        # project-specific phone projection into the same cached script URL. Existing
        # HTML and CSP therefore need no new script surface.
        content = (
            static_path("publishing.js").read_text(encoding="utf-8")
            + "\n"
            + _project_publishing_script()
        )
        response = Response(content, media_type="text/javascript; charset=utf-8")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


__all__ = ["install_publishing_pwa_route"]
