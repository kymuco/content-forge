"""FastAPI adapter for the packaged Content Forge PWA shell."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from content_forge.application import AuthManager

from . import static_path
from .onboarding import normalize_public_base_url, pairing_url, qr_svg

_PWA_CSP = (
    "default-src 'self'; "
    "img-src 'self' blob: data:; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)
_PWA_MAX_QUEUE_ENTRIES = 256
_PWA_MAX_BATCH_ENTRIES = 16
_PWA_MAX_FILENAME_CHARS = 1024
_PWA_MAX_MIME_CHARS = 255
_PWA_MAX_URL_CHARS = 4096
_PWA_MAX_NOTE_CHARS = 8192


def _route_relative_path(request: Request) -> str:
    path = str(request.scope.get("path") or "")
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if not root_path:
        return path
    if path == root_path:
        return "/"
    prefix = f"{root_path}/"
    if path.startswith(prefix):
        return path[len(root_path) :]
    return path


def _replace_route_relative_path(request: Request, route_path: str) -> None:
    """Rewrite only the internal ASGI route path while preserving a mount root."""

    current = str(request.scope.get("path") or "")
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if root_path and (current == root_path or current.startswith(f"{root_path}/")):
        rewritten = f"{root_path}{route_path}"
    else:
        rewritten = route_path
    request.scope["path"] = rewritten
    request.scope["raw_path"] = rewritten.encode("utf-8")


def _harden(response: Response, *, cache_control: str) -> Response:
    response.headers["Cache-Control"] = cache_control
    response.headers["Content-Security-Policy"] = _PWA_CSP
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _asset(name: str, media_type: str, *, cache_control: str = "no-cache") -> FileResponse:
    response = FileResponse(static_path(name), media_type=media_type)
    return _harden(response, cache_control=cache_control)  # type: ignore[return-value]


def _production_home_script() -> str:
    """Compose PR34 UI hooks into the proven PR33 phone controller deterministically."""

    source = static_path("production-home.js").read_text(encoding="utf-8")
    rendered_marker = "    projectFlowBody.appendChild(renderFinalStage(project));\n"
    closed_marker = "    setHidden(projectFlowPanel, true);\n    setHidden(panel, false);\n"
    if source.count(rendered_marker) != 1 or source.count(closed_marker) != 1:
        raise RuntimeError("production-home PR34 composition marker mismatch")
    source = source.replace(
        rendered_marker,
        rendered_marker
        + "    window.dispatchEvent(new CustomEvent(\"content-forge:project-flow-rendered\", { detail: { project } }));\n",
        1,
    )
    source = source.replace(
        closed_marker,
        "    window.dispatchEvent(new CustomEvent(\"content-forge:project-flow-closed\"));\n"
        + closed_marker,
        1,
    )
    return source


def install_pwa_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    pairing_bootstrap_allowed: Callable[[Request], bool],
    max_upload_bytes: int,
    share_body_limit: int,
    public_base_url: str | None = None,
) -> None:
    """Install the PR9 UI transport without moving Inbox semantics into HTTP routes."""

    if max_upload_bytes < 1 or share_body_limit < max_upload_bytes:
        raise ValueError("invalid PWA upload limits")
    configured_public_base_url: str | None = None
    if public_base_url is not None:
        configured_public_base_url = normalize_public_base_url(public_base_url)
    config_payload: dict[str, object] = {
        "maxUploadBytes": max_upload_bytes,
        "maxShareBodyBytes": share_body_limit,
        # Bound the persistent offline queue to at most one configured upload budget in
        # aggregate. Multiple smaller files can coexist, but browser storage cannot grow
        # beyond the server's configured single-upload authority without an explicit
        # future product decision to raise this independent limit.
        "maxQueueBytes": max_upload_bytes,
        "maxQueueEntries": _PWA_MAX_QUEUE_ENTRIES,
        "maxBatchEntries": _PWA_MAX_BATCH_ENTRIES,
        "maxFilenameChars": _PWA_MAX_FILENAME_CHARS,
        "maxMimeChars": _PWA_MAX_MIME_CHARS,
        "maxUrlChars": _PWA_MAX_URL_CHARS,
        "maxNoteChars": _PWA_MAX_NOTE_CHARS,
    }
    if configured_public_base_url is not None:
        config_payload["publicBaseUrl"] = configured_public_base_url
    config_script = (
        "self.CF_CONFIG = Object.freeze("
        + json.dumps(config_payload, sort_keys=True, separators=(",", ":"))
        + ");\n"
    )

    @app.middleware("http")
    async def pwa_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)

        # The browser shell extends the existing challenge action with an optional
        # public_url query. Route that request to the dedicated onboarding handler while
        # leaving ordinary PR8 challenge creation byte-for-byte compatible.
        if (
            request.method == "POST"
            and route_path == "/api/v1/pairing/challenges"
            and "public_url" in request.query_params
        ):
            _replace_route_relative_path(request, "/api/v1/pwa/onboarding")
            route_path = "/api/v1/pwa/onboarding"

        # A correctly installed Service Worker intercepts the Web Share Target before
        # the request reaches the server. This server fallback must never parse or ingest
        # an unauthenticated share body; it only bounds the request and returns guidance.
        if request.method == "POST" and route_path == "/app/share-target":
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return HTMLResponse("Share target requires Content-Length.", status_code=411)
            try:
                content_length = int(raw_length)
            except ValueError:
                return HTMLResponse("Invalid Content-Length.", status_code=400)
            if content_length < 0:
                return HTMLResponse("Invalid Content-Length.", status_code=400)
            if content_length > share_body_limit:
                return HTMLResponse("Shared payload exceeds the local upload limit.", status_code=413)
        return await call_next(request)

    @app.get("/app/", include_in_schema=False)
    def pwa_shell() -> FileResponse:
        return _asset("index.html", "text/html; charset=utf-8")

    @app.get("/app/config.js", include_in_schema=False)
    def pwa_config() -> Response:
        response = Response(config_script, media_type="text/javascript; charset=utf-8")
        return _harden(response, cache_control="no-cache")

    @app.get("/app/config.json", include_in_schema=False)
    def pwa_live_config() -> Response:
        # The active Service Worker reads this before an online Android share so a
        # server-side limit change becomes authority immediately, without waiting for a
        # worker update cycle. This endpoint is public for the same reason config.js is:
        # it contains limits only, never bearer/session material.
        response = JSONResponse(config_payload)
        return _harden(response, cache_control="no-store")

    @app.get("/app/styles.css", include_in_schema=False)
    def pwa_styles() -> FileResponse:
        return _asset("styles.css", "text/css; charset=utf-8")

    @app.get("/app/shared.js", include_in_schema=False)
    def pwa_shared_js() -> FileResponse:
        return _asset("shared.js", "text/javascript; charset=utf-8")

    @app.get("/app/app.js", include_in_schema=False)
    def pwa_app_js() -> FileResponse:
        return _asset("app.js", "text/javascript; charset=utf-8")

    @app.get("/app/production-home.js", include_in_schema=False)
    def pwa_production_home_js() -> Response:
        response = Response(
            _production_home_script(),
            media_type="text/javascript; charset=utf-8",
        )
        return _harden(response, cache_control="no-cache")

    @app.get("/app/sw.js", include_in_schema=False)
    def pwa_service_worker() -> FileResponse:
        response = _asset("sw.js", "text/javascript; charset=utf-8", cache_control="no-cache")
        response.headers["Service-Worker-Allowed"] = "./"
        return response

    @app.get("/app/manifest.webmanifest", include_in_schema=False)
    def pwa_manifest() -> FileResponse:
        return _asset("manifest.webmanifest", "application/manifest+json", cache_control="no-cache")

    @app.get("/app/icons/icon-192.png", include_in_schema=False)
    def pwa_icon_192() -> FileResponse:
        return _asset("icons/icon-192.png", "image/png", cache_control="public, max-age=86400")

    @app.get("/app/icons/icon-512.png", include_in_schema=False)
    def pwa_icon_512() -> FileResponse:
        return _asset("icons/icon-512.png", "image/png", cache_control="public, max-age=86400")

    @app.post("/app/share-target", include_in_schema=False)
    def share_target_without_worker() -> HTMLResponse:
        response = HTMLResponse(
            "<!doctype html><title>Content Forge</title>"
            "<p>Open Content Forge once so its Service Worker can receive Android shares, then share again.</p>",
            status_code=409,
        )
        return _harden(response, cache_control="no-store")  # type: ignore[return-value]

    @app.post("/api/v1/pwa/onboarding", status_code=201)
    def create_pwa_onboarding(
        request: Request,
        public_url: str = Query(min_length=1, max_length=2048),
    ) -> dict[str, object]:
        if not pairing_bootstrap_allowed(request):
            raise HTTPException(
                status_code=403,
                detail="PWA onboarding requires loopback client, Host, and Origin",
            )
        try:
            normalized = normalize_public_base_url(public_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        challenge = auth.create_challenge()
        url = pairing_url(
            normalized,
            challenge_id=challenge.challenge_id,
            code=challenge.code,
        )
        payload = challenge.model_dump(mode="json")
        payload.update(
            {
                "public_url": normalized,
                "pairing_url": url,
                "qr_svg": qr_svg(url),
            }
        )
        return payload


__all__ = ["install_pwa_routes"]
