"""Authenticated PR19 dialogue, character, and speaker-assignment HTTP surface."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from content_forge.application import (
    AuthManager,
    AuthenticationError,
    AuthSession,
    CharacterRecord,
    DialogueAssignment,
    DialogueAssignmentSuggestion,
    DialogueConflictError,
    DialogueError,
    DialogueNotFoundError,
    DialogueValidationError,
    DialogueWorkflow,
)
from content_forge.storage import LocalLibrary
from content_forge.web import static_path

from .app import _transport_is_secure

_DIALOGUE_JSON_BODY_LIMIT = 512 * 1024


class DialoguePrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: tuple[DialogueAssignmentSuggestion, ...] = Field(default=(), max_length=32)


class DialogueAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment: DialogueAssignment


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


def _dialogue_http_error(exc: DialogueError) -> HTTPException:
    if isinstance(exc, DialogueNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DialogueConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, DialogueValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="dialogue workflow failed")


def _project_payload(workflow: DialogueWorkflow, project_id: str) -> dict[str, object]:
    project, _ = workflow._snapshot(project_id)
    return {
        "project_id": project.project_id,
        "project_state": project.state.value,
        "dialogue": workflow.manifest(project_id).model_dump(mode="json"),
    }


def install_dialogue_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
) -> DialogueWorkflow:
    """Install PR19 routes with transport/auth/body gates before body parsing."""

    workflow = DialogueWorkflow(library)
    app.state.dialogue = workflow

    @app.middleware("http")
    async def pr19_dialogue_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not route_path.startswith("/api/v1/dialogue/"):
            return await call_next(request)

        # This middleware is installed after the PR8 app middleware and therefore runs
        # outside it. Preserve the global transport invariant here before authentication
        # or body-policy responses can reveal dialogue-route behavior over plaintext LAN.
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
                    content={"detail": "Content-Length is required for dialogue bodies"},
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length > _DIALOGUE_JSON_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "dialogue request body exceeds limit"},
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

    @app.get("/app/dialogue.js", include_in_schema=False)
    def dialogue_script() -> FileResponse:
        response = FileResponse(
            static_path("dialogue.js"),
            media_type="text/javascript; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/dialogue/review-queue")
    def dialogue_review_queue(
        _session: AuthSession = Depends(require_session),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        try:
            return workflow.list_queue(limit=limit)
        except DialogueError as exc:
            raise _dialogue_http_error(exc) from exc

    @app.get("/api/v1/dialogue/projects/{project_id}")
    def dialogue_project(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return _project_payload(workflow, project_id)
        except DialogueError as exc:
            raise _dialogue_http_error(exc) from exc

    @app.post("/api/v1/dialogue/projects/{project_id}/characters", status_code=201)
    def register_character(
        project_id: str,
        character: CharacterRecord,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            workflow.register_character(project_id, character)
            return _project_payload(workflow, project_id)
        except DialogueError as exc:
            raise _dialogue_http_error(exc) from exc

    @app.put("/api/v1/dialogue/projects/{project_id}/characters/{character_id}")
    def update_character(
        project_id: str,
        character_id: str,
        character: CharacterRecord,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        if character.character_id != character_id:
            raise HTTPException(status_code=422, detail="character path identity mismatch")
        try:
            workflow.update_character(project_id, character)
            return _project_payload(workflow, project_id)
        except DialogueError as exc:
            raise _dialogue_http_error(exc) from exc

    @app.post("/api/v1/dialogue/projects/{project_id}/scenes/{scene_id}/prepare")
    def prepare_scene_assignment(
        project_id: str,
        scene_id: str,
        payload: DialoguePrepareRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            project = workflow.prepare_scene_assignment(
                project_id,
                scene_id,
                suggestions=payload.suggestions,
            )
            return {
                "project_id": project.project_id,
                "project_state": project.state.value,
                "dialogue": workflow.manifest(project_id).model_dump(mode="json"),
            }
        except DialogueError as exc:
            raise _dialogue_http_error(exc) from exc

    @app.post("/api/v1/dialogue/projects/{project_id}/tasks/{task_id}/assign")
    def assign_scene_dialogue(
        project_id: str,
        task_id: str,
        payload: DialogueAssignRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            project = workflow.apply_scene_assignment(
                project_id,
                task_id,
                payload.assignment,
            )
            return {
                "project_id": project.project_id,
                "project_state": project.state.value,
                "dialogue": workflow.manifest(project_id).model_dump(mode="json"),
            }
        except DialogueError as exc:
            raise _dialogue_http_error(exc) from exc

    return workflow


__all__ = ["install_dialogue_routes"]
