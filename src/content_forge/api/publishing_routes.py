"""Authenticated PR27/PR29 publishing approval and execution transport."""

from __future__ import annotations

from threading import Lock

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from content_forge.application import AuthManager, AuthenticationError, AuthSession
from content_forge.application.publishing import (
    PublishArtifactError,
    PublishAttemptError,
    PublishOrchestrationError,
    PublishOutcomeUnknownError,
    PublishingService,
)
from content_forge.core import EntityKind, require_entity_id
from content_forge.providers import (
    PublishContractVersion,
    PublishDeclarations,
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    PublishingExecutionError,
    PublishingProvider,
    PublishingResponseError,
    PublishingUnavailableError,
    publish_idempotency_key,
    semantic_publish_request_digest,
)
from content_forge.storage import LocalLibrary, PublishAttemptRecord, StorageConflictError

from .app import _transport_is_secure

_PUBLISHING_JSON_BODY_LIMIT = 64 * 1024
_PUBLISHING_JSON_ROUTES = frozenset(
    {
        "/api/v1/publishing/candidates",
        "/api/v1/publishing/attempts",
    }
)


class PublishCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_job_id: str
    target: PublishTarget
    metadata: PublishMetadata
    contract_version: PublishContractVersion = "pr27_publish_contract_v1"
    declarations: PublishDeclarations | None = None

    @field_validator("render_job_id")
    @classmethod
    def validate_render_job_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.JOB)

    @model_validator(mode="after")
    def validate_contract_shape(self):
        if self.contract_version == "pr27_publish_contract_v1":
            if self.declarations is not None:
                raise ValueError("v1 publish candidates cannot contain publication declarations")
            return self
        if self.declarations is None:
            raise ValueError("v2 publish candidates require explicit publication declarations")
        return self


class PublishApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: PublishRequest
    confirm_request_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    note: str | None = Field(default=None, max_length=4096)


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


def _attempt_payload(library: LocalLibrary, attempt: PublishAttemptRecord) -> dict[str, object]:
    approved = library.publishing.approved_request(attempt.attempt_id)
    operation = library.publishing.get_operation(attempt.request_sha256)
    if operation is None:
        raise HTTPException(status_code=500, detail="publish attempt operation is missing")
    return {
        "attempt": attempt.model_dump(mode="json"),
        "request": approved.request.model_dump(mode="json"),
        "request_sha256": attempt.request_sha256,
        "idempotency_key": operation.idempotency_key,
    }


def _publishing_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PublishArtifactError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PublishOutcomeUnknownError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PublishAttemptError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, StorageConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PublishingUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (PublishingExecutionError, PublishingResponseError)):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, PublishOrchestrationError):
        return HTTPException(status_code=500, detail="publishing workflow failed")
    return HTTPException(status_code=500, detail="publishing workflow failed")


def install_publishing_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
    provider: PublishingProvider | None = None,
    ffprobe_path: str = "ffprobe",
) -> PublishingService:
    """Install publishing routes without making a publishing provider mandatory."""

    service = PublishingService(
        library,
        provider,
        ffprobe_path=ffprobe_path,
    )
    app.state.publishing = service
    reconciled = False
    reconciliation_lock = Lock()

    def ensure_reconciled() -> None:
        nonlocal reconciled
        with reconciliation_lock:
            if reconciled:
                return
            service.reconcile_interrupted()
            reconciled = True

    @app.middleware("http")
    async def publishing_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not (
            route_path == "/api/v1/publishing"
            or route_path.startswith("/api/v1/publishing/")
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

        if request.method == "POST" and route_path in _PUBLISHING_JSON_ROUTES:
            content_type = request.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return JSONResponse(
                    status_code=415,
                    content={"detail": "application/json is required"},
                )
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return JSONResponse(
                    status_code=411,
                    content={"detail": "Content-Length is required for publishing bodies"},
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length > _PUBLISHING_JSON_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "publishing request body exceeds limit"},
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

    @app.get("/api/v1/publishing/status")
    def publishing_status(
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        ensure_reconciled()
        return {
            "provider_configured": provider is not None,
            "remote_execution_enabled": provider is not None,
            "preferred_contract_version": "pr29_publish_contract_v2",
        }

    @app.post("/api/v1/publishing/candidates")
    def build_candidate(
        payload: PublishCandidateInput,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            request = service.candidate(
                payload.render_job_id,
                target=payload.target,
                metadata=payload.metadata,
                contract_version=payload.contract_version,
                declarations=payload.declarations,
            )
        except Exception as exc:
            raise _publishing_http_error(exc) from exc
        return {
            "request": request.model_dump(mode="json"),
            "request_sha256": semantic_publish_request_digest(request),
            "idempotency_key": publish_idempotency_key(request),
            "provider_configured": provider is not None,
        }

    @app.post("/api/v1/publishing/attempts", status_code=201)
    def approve_candidate(
        payload: PublishApprovalInput,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        expected = semantic_publish_request_digest(payload.request)
        if payload.confirm_request_sha256.lower() != expected:
            raise HTTPException(
                status_code=422,
                detail="publish approval digest does not match exact request",
            )
        ensure_reconciled()
        try:
            attempt = service.approve(
                payload.request,
                confirm_request_sha256=expected,
                note=payload.note,
            )
            return _attempt_payload(library, attempt)
        except Exception as exc:
            raise _publishing_http_error(exc) from exc

    @app.get("/api/v1/publishing/attempts/{attempt_id}")
    def get_attempt(
        attempt_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            attempt_id = require_entity_id(attempt_id, EntityKind.PUBLISH)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ensure_reconciled()
        attempt = library.publishing.get_attempt(attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="publish attempt not found")
        return _attempt_payload(library, attempt)

    @app.post("/api/v1/publishing/attempts/{attempt_id}/execute")
    def execute_attempt(
        attempt_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            attempt_id = require_entity_id(attempt_id, EntityKind.PUBLISH)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ensure_reconciled()
        if library.publishing.get_attempt(attempt_id) is None:
            raise HTTPException(status_code=404, detail="publish attempt not found")
        try:
            attempt = service.execute_prepared(attempt_id)
            return _attempt_payload(library, attempt)
        except Exception as exc:
            raise _publishing_http_error(exc) from exc

    return service


__all__ = [
    "PublishApprovalInput",
    "PublishCandidateInput",
    "install_publishing_routes",
]
