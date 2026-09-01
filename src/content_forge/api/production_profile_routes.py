"""Authenticated PR25 production-profile registry and Project binding surface."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from content_forge.application import (
    AuthManager,
    AuthenticationError,
    AuthSession,
    ProductionProfileConflictError,
    ProductionProfileDefinition,
    ProductionProfileError,
    ProductionProfileNotFoundError,
    ProductionProfileValidationError,
    ProductionProfileWorkflow,
)
from content_forge.storage import LocalLibrary
from content_forge.templates import create_builtin_registries

from .app import _transport_is_secure

_PRODUCTION_PROFILE_JSON_BODY_LIMIT = 256 * 1024


class ProductionProfileBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    revision: int | None = Field(default=None, ge=1)


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


def _authorization_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise AuthenticationError("bearer token required")
    token = value[7:].strip()
    if not token:
        raise AuthenticationError("bearer token required")
    return token


def _profile_http_error(exc: ProductionProfileError) -> HTTPException:
    if isinstance(exc, ProductionProfileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ProductionProfileConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ProductionProfileValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="production profile workflow failed")


def _project_payload(
    workflow: ProductionProfileWorkflow,
    project_id: str,
) -> dict[str, object]:
    project, _ = workflow._snapshot(project_id)
    manifest = workflow.manifest(project_id)
    return {
        "project_id": project.project_id,
        "project_state": project.state.value,
        "template": None if project.template is None else project.template.model_dump(mode="json"),
        "output_profiles": [item.model_dump(mode="json") for item in project.output_profiles],
        "profile": None if manifest is None else manifest.model_dump(mode="json"),
    }


def install_production_profile_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
) -> ProductionProfileWorkflow:
    """Install PR25 routes with auth/body gates before JSON parsing."""

    workflow = ProductionProfileWorkflow(library, create_builtin_registries())
    app.state.production_profiles = workflow

    @app.middleware("http")
    async def pr25_production_profile_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not (
            route_path == "/api/v1/production-profiles"
            or route_path.startswith("/api/v1/production-profiles/")
        ):
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

        if request.method in {"POST", "PUT", "PATCH"}:
            content_type = request.headers.get("content-type", "")
            if not content_type.lower().startswith("application/json"):
                return JSONResponse(
                    status_code=415,
                    content={"detail": "application/json is required"},
                )
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return JSONResponse(
                    status_code=411,
                    content={"detail": "Content-Length is required for production profile bodies"},
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length > _PRODUCTION_PROFILE_JSON_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "production profile request body exceeds limit"},
                )
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

    @app.get("/api/v1/production-profiles")
    def list_profiles(
        limit: int = Query(default=256, ge=1, le=256),
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return {
                "items": [
                    item.model_dump(mode="json")
                    for item in workflow.registry.list_latest(limit=limit)
                ]
            }
        except ProductionProfileError as exc:
            raise _profile_http_error(exc) from exc

    @app.post("/api/v1/production-profiles", status_code=status.HTTP_201_CREATED)
    def put_profile(
        definition: ProductionProfileDefinition,
        response: Response,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            previous = workflow.registry.repository.get(definition.profile_id)
            revision = workflow.registry.put(definition)
            if previous is not None and previous == revision:
                response.status_code = status.HTTP_200_OK
            return revision.model_dump(mode="json")
        except ProductionProfileError as exc:
            raise _profile_http_error(exc) from exc

    @app.get("/api/v1/production-profiles/registry/{profile_id}")
    def get_profile(
        profile_id: str,
        revision: int | None = Query(default=None, ge=1),
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return workflow.registry.get(profile_id, revision).model_dump(mode="json")
        except ProductionProfileError as exc:
            raise _profile_http_error(exc) from exc

    @app.get("/api/v1/production-profiles/projects/{project_id}")
    def project_profile(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return _project_payload(workflow, project_id)
        except ProductionProfileError as exc:
            raise _profile_http_error(exc) from exc

    @app.put("/api/v1/production-profiles/projects/{project_id}")
    def bind_project_profile(
        project_id: str,
        payload: ProductionProfileBindRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            workflow.bind(
                project_id,
                payload.profile_id,
                revision=payload.revision,
            )
            return _project_payload(workflow, project_id)
        except ProductionProfileError as exc:
            raise _profile_http_error(exc) from exc

    @app.delete("/api/v1/production-profiles/projects/{project_id}")
    def unbind_project_profile(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            workflow.unbind(project_id)
            return _project_payload(workflow, project_id)
        except ProductionProfileError as exc:
            raise _profile_http_error(exc) from exc

    return workflow


__all__ = ["ProductionProfileBindRequest", "install_production_profile_routes"]
