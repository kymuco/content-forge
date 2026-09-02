"""Authenticated PR32 phone production-preset surface."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from content_forge.application import AuthManager, AuthenticationError, AuthSession
from content_forge.application.production_presets import (
    ProductionPresetConflictError,
    ProductionPresetError,
    ProductionPresetNotFoundError,
    ProductionPresetService,
    ProductionPresetValidationError,
)
from content_forge.application.review import ReviewError, ReviewService
from content_forge.storage import LocalLibrary

from .app import _transport_is_secure

_PRODUCTION_PRESET_JSON_BODY_LIMIT = 32 * 1024


class ProductionProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=36, max_length=36)
    preset_id: str = Field(min_length=1, max_length=64)
    source_project_ids: tuple[str, ...] = Field(min_length=1, max_length=64)


def _authorization_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise AuthenticationError("bearer token required")
    token = value[7:].strip()
    if not token:
        raise AuthenticationError("bearer token required")
    return token


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


def _preset_http_error(exc: ProductionPresetError) -> HTTPException:
    if isinstance(exc, ProductionPresetNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ProductionPresetConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ProductionPresetValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="production preset workflow failed")


def install_production_preset_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
    review: ReviewService,
) -> ProductionPresetService:
    service = ProductionPresetService(library)
    app.state.production_presets = service

    @app.middleware("http")
    async def pr32_production_preset_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not route_path.startswith("/api/v1/production/"):
            return await call_next(request)
        if not _transport_is_secure(request):
            return JSONResponse(
                status_code=426,
                content={"detail": "non-loopback requests require HTTPS"},
            )
        try:
            token = _authorization_token(request.headers.get("authorization"))
            auth.authenticate(token)
        except AuthenticationError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})
        if request.method == "POST":
            content_type = request.headers.get("content-type", "")
            if not content_type.lower().startswith("application/json"):
                return JSONResponse(status_code=415, content={"detail": "application/json is required"})
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return JSONResponse(status_code=411, content={"detail": "Content-Length is required"})
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length > _PRODUCTION_PRESET_JSON_BODY_LIMIT:
                return JSONResponse(status_code=413, content={"detail": "production preset request body exceeds limit"})
        return await call_next(request)

    def bearer_token(authorization: str | None = Header(default=None)) -> str:
        try:
            return _authorization_token(authorization)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def require_session(token: str = Depends(bearer_token)) -> AuthSession:
        try:
            return auth.authenticate(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/v1/production/presets")
    def list_presets(_session: AuthSession = Depends(require_session)) -> dict[str, object]:
        return {"items": [item.payload() for item in service.list_presets()]}

    @app.get("/api/v1/production/sources")
    def list_sources(
        limit: int = Query(default=100, ge=1, le=500),
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return {"items": list(service.list_sources(limit=limit))}
        except ProductionPresetError as exc:
            raise _preset_http_error(exc) from exc

    @app.post("/api/v1/production/projects", status_code=status.HTTP_201_CREATED)
    def create_production_project(
        payload: ProductionProjectCreateRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            project = service.create_project(
                request_id=payload.request_id,
                preset_id=payload.preset_id,
                source_project_ids=payload.source_project_ids,
            )
            prepared = review.bootstrap_project(project.project_id)
            return review.project_summary(prepared)
        except ProductionPresetError as exc:
            raise _preset_http_error(exc) from exc
        except ReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return service


__all__ = ["ProductionProjectCreateRequest", "install_production_preset_routes"]
