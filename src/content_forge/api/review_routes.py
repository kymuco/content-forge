"""Authenticated PR10 review, preview, and final-render HTTP surface."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from content_forge.application import AuthManager, AuthenticationError, AuthSession
from content_forge.application.review import (
    ReviewConflictError,
    ReviewError,
    ReviewNotFoundError,
    ReviewNotReadyError,
    ReviewRenderError,
    ReviewService,
    ReviewValidationError,
)
from content_forge.storage import LocalLibrary
from content_forge.web import static_path

_REVIEW_JSON_BODY_LIMIT = 128 * 1024


class ReviewResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: JsonValue | None = None


class ReviewRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback: str | None = Field(default=None, max_length=4096)


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


def _is_review_route(path: str) -> bool:
    return (
        path == "/api/v1/review-queue"
        or path.startswith("/api/v1/projects/")
        or path.startswith("/api/v1/render-jobs/")
    )


def _has_json_body(path: str, method: str) -> bool:
    return method == "POST" and (path.endswith("/resolve") or path.endswith("/reject"))


def _review_http_error(exc: ReviewError) -> HTTPException:
    if isinstance(exc, ReviewNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ReviewConflictError, ReviewNotReadyError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ReviewValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ReviewRenderError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail="review workflow failed")


def install_review_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> ReviewService:
    """Install PR10 routes without granting HTTP handlers renderer authority."""

    review = ReviewService(
        library,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    app.state.review = review

    @app.middleware("http")
    async def pr10_review_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not _is_review_route(route_path):
            return await call_next(request)

        # Auth must win before FastAPI/Pydantic parses any review mutation body.
        try:
            token = _authorization_token(request.headers.get("authorization"))
            auth.authenticate(token)
        except AuthenticationError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})

        if _has_json_body(route_path, request.method):
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
                    content={"detail": "Content-Length is required for review bodies"},
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "invalid Content-Length"},
                )
            if content_length < 0:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "invalid Content-Length"},
                )
            if content_length > _REVIEW_JSON_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "review request body exceeds limit"},
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

    @app.get("/app/review.js", include_in_schema=False)
    def review_script() -> FileResponse:
        response = FileResponse(
            static_path("review.js"),
            media_type="text/javascript; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/review-queue")
    def review_queue(
        _session: AuthSession = Depends(require_session),
        limit: int = Query(default=100, ge=1, le=500),
        include_auto: bool = False,
    ) -> dict[str, object]:
        try:
            return review.list_queue(limit=limit, include_auto=include_auto)
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.get("/api/v1/projects/{project_id}")
    def project_review_summary(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return review.project_summary(review.get_project(project_id))
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.post("/api/v1/projects/{project_id}/review/bootstrap")
    def bootstrap_review(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return review.project_summary(review.bootstrap_project(project_id))
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.post("/api/v1/projects/{project_id}/review/{task_id}/resolve")
    def resolve_review_task(
        project_id: str,
        task_id: str,
        payload: ReviewResolveRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return review.project_summary(
                review.resolve_task(project_id, task_id, payload.value)
            )
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.post("/api/v1/projects/{project_id}/preview")
    def render_preview(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return review.render_preview(project_id)
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.post("/api/v1/projects/{project_id}/preview/{job_id}/approve")
    def approve_preview(
        project_id: str,
        job_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return review.project_summary(review.approve_preview(project_id, job_id))
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.post("/api/v1/projects/{project_id}/preview/{job_id}/reject")
    def reject_preview(
        project_id: str,
        job_id: str,
        payload: ReviewRejectRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return review.project_summary(
                review.reject_preview(project_id, job_id, feedback=payload.feedback)
            )
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.post("/api/v1/projects/{project_id}/final")
    def render_final(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return review.render_final(project_id)
        except ReviewError as exc:
            raise _review_http_error(exc) from exc

    @app.get("/api/v1/render-jobs/{job_id}/artifact")
    def render_artifact(
        job_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> FileResponse:
        try:
            artifact, path = review.artifact_path(job_id)
        except ReviewError as exc:
            raise _review_http_error(exc) from exc
        response = FileResponse(
            Path(path),
            media_type="video/mp4",
            filename=f"{artifact.purpose}-{artifact.job_id}.mp4",
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return review


__all__ = ["install_review_routes"]
