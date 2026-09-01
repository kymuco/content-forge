"""Authenticated PR23 voiced-scene presentation preview/materialization surface."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from content_forge.application import (
    AuthManager,
    AuthenticationError,
    AuthSession,
    DialogueConflictError,
    DialogueError,
    DialogueNotFoundError,
    DialogueValidationError,
    VoicedSceneConflictError,
    VoicedSceneError,
    VoicedSceneNotFoundError,
    VoicedSceneNotReadyError,
    VoicedScenePreset,
    VoicedSceneValidationError,
    VoicedSceneWorkflow,
    VoicedStoryConflictError,
    VoicedStoryError,
    VoicedStoryNotFoundError,
    VoicedStoryNotReadyError,
    VoicedStoryValidationError,
)
from content_forge.storage import LocalLibrary
from content_forge.web import static_path

from .app import _transport_is_secure

_VOICED_SCENE_JSON_BODY_LIMIT = 64 * 1024


class MaterializeVoicedSceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: VoicedScenePreset | None = None


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


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (VoicedSceneNotFoundError, VoicedStoryNotFoundError, DialogueNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (
            VoicedSceneNotReadyError,
            VoicedSceneConflictError,
            VoicedStoryNotReadyError,
            VoicedStoryConflictError,
            DialogueConflictError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(
        exc,
        (VoicedSceneValidationError, VoicedStoryValidationError, DialogueValidationError),
    ):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (VoicedSceneError, VoicedStoryError, DialogueError)):
        return HTTPException(status_code=500, detail="voiced scene workflow failed")
    return HTTPException(status_code=500, detail="voiced scene workflow failed")


def install_voiced_scene_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
) -> VoicedSceneWorkflow:
    """Install PR23 presentation preview/materialization with auth before JSON parsing."""

    workflow = VoicedSceneWorkflow(library)
    app.state.voiced_scene = workflow

    @app.middleware("http")
    async def pr23_voiced_scene_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not (
            route_path == "/api/v1/voiced-scene"
            or route_path.startswith("/api/v1/voiced-scene/")
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
                    content={"detail": "Content-Length is required for voiced scene bodies"},
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length > _VOICED_SCENE_JSON_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "voiced scene request body exceeds limit"},
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

    @app.get("/app/voiced-scene.js", include_in_schema=False)
    def voiced_scene_script() -> FileResponse:
        response = FileResponse(
            static_path("voiced-scene.js"),
            media_type="text/javascript; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/voiced-scene/projects/{project_id}/preview")
    def preview_scene(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return workflow.preview(project_id).model_dump(mode="json")
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/v1/voiced-scene/projects/{project_id}")
    def current_scene(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return workflow.manifest(project_id).model_dump(mode="json")
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/voiced-scene/projects/{project_id}/materialize")
    def materialize_scene(
        project_id: str,
        payload: MaterializeVoicedSceneRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return workflow.materialize(
                project_id,
                preset=payload.preset,
            ).model_dump(mode="json")
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/v1/voiced-scene/projects/{project_id}/materialization")
    def dematerialize_scene(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> Response:
        try:
            workflow.dematerialize(project_id)
            return Response(status_code=204)
        except Exception as exc:
            raise _http_error(exc) from exc

    return workflow


__all__ = ["install_voiced_scene_routes"]
