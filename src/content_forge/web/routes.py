"""FastAPI adapter for the packaged Content Forge PWA shell."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

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


def _harden(response: FileResponse | HTMLResponse, *, cache_control: str) -> FileResponse | HTMLResponse:
    response.headers["Cache-Control"] = cache_control
    response.headers["Content-Security-Policy"] = _PWA_CSP
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _asset(name: str, media_type: str, *, cache_control: str = "no-cache") -> FileResponse:
    response = FileResponse(static_path(name), media_type=media_type)
    return _harden(response, cache_control=cache_control)  # type: ignore[return-value]


def install_pwa_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    pairing_bootstrap_allowed: Callable[[Request], bool],
    share_body_limit: int,
) -> None:
    """Install the PR9 UI transport without moving Inbox semantics into HTTP routes."""

    @app.middleware("http")
    async def bound_unhandled_share_target(request: Request, call_next):
        route_path = _route_relative_path(request)
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

    @app.get("/app/styles.css", include_in_schema=False)
    def pwa_styles() -> FileResponse:
        return _asset("styles.css", "text/css; charset=utf-8")

    @app.get("/app/shared.js", include_in_schema=False)
    def pwa_shared_js() -> FileResponse:
        return _asset("shared.js", "text/javascript; charset=utf-8")

    @app.get("/app/app.js", include_in_schema=False)
    def pwa_app_js() -> FileResponse:
        return _asset("app.js", "text/javascript; charset=utf-8")

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
