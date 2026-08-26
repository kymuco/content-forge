"""Authenticated local FastAPI transport for Content Forge application services."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from content_forge.application import (
    ApplicationRepository,
    AuthManager,
    AuthenticationError,
    AuthSession,
    InboxError,
    InboxIntake,
    InboxService,
    UploadTooLargeError,
)
from content_forge.application.idempotency import (
    IdempotencyConflict,
    IdempotencyReplay,
    intake_idempotency_scope,
    normalize_idempotency_key,
)
from content_forge.application.runtime_lock import RuntimeLease
from content_forge.core import RegistryKey
from content_forge.storage import LocalLibrary

PAIRING_ID_PATTERN = r"^cf_pair_[0-9a-f]{32}$"
PAIRING_CODE_PATTERN = r"^[0-9]{8}$"
MULTIPART_OVERHEAD_BUDGET = 1024 * 1024
PARSED_BODY_LIMIT = 128 * 1024
_PREPARSE_AUTH_POST_ROUTES = frozenset(
    {
        "/api/v1/inbox/files",
        "/api/v1/inbox/url-note",
    }
)
_BOUNDED_PARSED_POST_ROUTES = frozenset(
    {
        "/api/v1/pairing/exchange",
        "/api/v1/inbox/url-note",
    }
)


class PairExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(
        min_length=40,
        max_length=40,
        pattern=PAIRING_ID_PATTERN,
    )
    code: str = Field(
        min_length=8,
        max_length=8,
        pattern=PAIRING_CODE_PATTERN,
    )
    label: str | None = Field(default=None, max_length=256)


class PairExchangeResponse(BaseModel):
    session_id: str
    token: str
    expires_at: str


class URLNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str | None = Field(default=None, max_length=4096)
    note: str | None = Field(default=None, max_length=8192)
    creator_hint: str | None = Field(default=None, max_length=512)
    content_kind_hint: RegistryKey | None = None

    @model_validator(mode="after")
    def require_payload(self):
        if not (self.source_url or self.note):
            raise ValueError("source_url or note is required")
        return self


def _intake_payload(intake: InboxIntake) -> dict[str, object]:
    return intake.model_dump(mode="json")


def _is_loopback_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_loopback_client(request: Request) -> bool:
    if request.client is None:
        return False
    host = request.client.host
    if host == "testclient":
        return True
    return _is_loopback_hostname(host)


def _host_header_is_loopback(value: str | None) -> bool:
    if not value or "@" in value or "/" in value or "\\" in value:
        return False
    try:
        hostname = urlsplit(f"//{value}").hostname
    except ValueError:
        return False
    return _is_loopback_hostname(hostname)


def _origin_is_loopback(value: str | None) -> bool:
    if value is None:
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and _is_loopback_hostname(parsed.hostname)


def _pairing_bootstrap_allowed(request: Request) -> bool:
    """Require socket loopback plus browser-visible loopback authority.

    The peer address alone is insufficient because browsers can reach loopback and DNS
    rebinding can preserve a hostile origin while the hostname resolves to 127.0.0.1.
    Host and (when present) Origin must therefore also identify loopback authority.
    """

    return (
        _is_loopback_client(request)
        and _host_header_is_loopback(request.headers.get("host"))
        and _origin_is_loopback(request.headers.get("origin"))
    )


def _transport_is_secure(request: Request) -> bool:
    """Permit plaintext only from a loopback socket peer.

    CLI validation prevents the supported server entry point from binding a LAN socket
    without TLS, but `create_app()` is also a public ASGI factory. Enforce the same
    fail-closed rule from the request scope so a programmatic Uvicorn/ASGI deployment
    cannot expose pairing responses or bearer credentials over plaintext LAN HTTP.
    """

    return _is_loopback_client(request) or request.url.scheme == "https"


def _route_relative_path(request: Request) -> str:
    """Return the app-relative ASGI path even when mounted under a root path.

    `request.url.path` includes `root_path`, while Starlette routing accounts for that
    prefix separately. Pre-parser security gates must therefore normalize the ASGI scope
    before matching an endpoint; otherwise mounting the app could bypass those gates.
    """

    path = str(request.scope.get("path") or "")
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if not root_path:
        return path
    if path == root_path:
        return "/"
    prefix = f"{root_path}/"
    if path.startswith(prefix):
        return path[len(root_path) :]
    # Some ASGI integrations already provide a route-relative `path` alongside a
    # nonempty root_path. Do not strip an unrelated prefix in that case.
    return path


def _authorization_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise AuthenticationError("bearer token required")
    token = value[7:].strip()
    if not token:
        raise AuthenticationError("bearer token required")
    return token


def create_app(
    *,
    root: str | Path | None = None,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024,
) -> FastAPI:
    library = LocalLibrary(root)
    runtime_lease = RuntimeLease.acquire(library.paths.root / ".api-runtime.lock")
    try:
        repository = ApplicationRepository(library.database).initialize()
        auth = AuthManager(repository)
        inbox = InboxService(
            library,
            repository,
            ffprobe_path=ffprobe_path,
            ffmpeg_path=ffmpeg_path,
            max_upload_bytes=max_upload_bytes,
        )
        # Reconciliation runs only after exclusive ownership of this runtime root has
        # been established. A second live API process therefore cannot mistake an active
        # uploader's RECEIVING receipt for an interrupted operation.
        inbox.reconcile_receiving()

        app = FastAPI(
            title="Content Forge Local API",
            version="0.0.1",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
    except BaseException:
        runtime_lease.close()
        raise

    app.state.library = library
    app.state.application_repository = repository
    app.state.auth = auth
    app.state.inbox = inbox
    # The lease is intentionally process-scoped. Keeping it reachable from app.state
    # holds the OS advisory lock for the API object's lifetime; process exit releases it
    # unconditionally, including crashes. Tests/tools that intentionally replace a live
    # app in-process can explicitly close this narrow ownership object first.
    app.state.runtime_lease = runtime_lease

    @app.middleware("http")
    async def protect_transport_and_request_bodies(request: Request, call_next):
        """Enforce transport, pre-parser auth, and bounded parsed request bodies."""

        if not _transport_is_secure(request):
            return JSONResponse(
                status_code=426,
                content={"detail": "non-loopback requests require HTTPS"},
            )

        route_path = _route_relative_path(request)
        if request.method == "POST":
            # Any authenticated route with a body must authenticate before FastAPI/
            # Starlette parses or buffers that body. This prevents an unauthenticated
            # caller from consuming multipart temp space or JSON body memory first.
            if route_path in _PREPARSE_AUTH_POST_ROUTES:
                try:
                    token = _authorization_token(request.headers.get("authorization"))
                    auth.authenticate(token)
                except AuthenticationError as exc:
                    return JSONResponse(status_code=401, content={"detail": str(exc)})

            body_limit: int | None = None
            body_kind = "request"
            if route_path == "/api/v1/inbox/files":
                body_limit = max_upload_bytes + MULTIPART_OVERHEAD_BUDGET
                body_kind = "multipart request"
            elif route_path in _BOUNDED_PARSED_POST_ROUTES:
                # Pairing exchange is intentionally unauthenticated, while URL/note is
                # authenticated above. Both are Pydantic parsed-body routes, so bound
                # them before call_next rather than after Starlette has buffered JSON.
                body_limit = PARSED_BODY_LIMIT
                body_kind = "request body"

            if body_limit is not None:
                raw_length = request.headers.get("content-length")
                if raw_length is None:
                    return JSONResponse(
                        status_code=411,
                        content={"detail": "Content-Length is required for request bodies"},
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
                if content_length > body_limit:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"{body_kind} exceeds request limit"},
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

    def intake_idempotency_key(
        value: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> str | None:
        if value is None:
            return None
        try:
            return normalize_idempotency_key(value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "service": "content-forge", "api_version": "v1"}

    @app.post("/api/v1/pairing/challenges", status_code=201)
    def create_pairing_challenge(request: Request) -> dict[str, object]:
        if not _pairing_bootstrap_allowed(request):
            raise HTTPException(
                status_code=403,
                detail="pairing challenges require loopback client, Host, and Origin",
            )
        challenge = auth.create_challenge()
        return challenge.model_dump(mode="json")

    @app.post("/api/v1/pairing/exchange", response_model=PairExchangeResponse)
    def exchange_pairing(payload: PairExchangeRequest) -> PairExchangeResponse:
        try:
            issued = auth.exchange(
                payload.challenge_id,
                payload.code,
                label=payload.label,
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return PairExchangeResponse(
            session_id=issued.session.session_id,
            token=issued.token,
            expires_at=issued.session.expires_at.isoformat(),
        )

    @app.delete("/api/v1/sessions/current", status_code=204)
    def revoke_current_session(
        token: str = Depends(bearer_token),
        _session: AuthSession = Depends(require_session),
    ) -> None:
        try:
            auth.revoke(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/v1/inbox")
    def list_inbox(
        _session: AuthSession = Depends(require_session),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        items = inbox.list_intakes(limit=limit)
        return {"items": [_intake_payload(item) for item in items]}

    @app.get("/api/v1/inbox/{intake_id}")
    def get_inbox_item(
        intake_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        intake = inbox.get_intake(intake_id)
        if intake is None:
            raise HTTPException(status_code=404, detail="intake not found")
        return _intake_payload(intake)

    @app.post("/api/v1/inbox/url-note", status_code=201)
    def capture_url_note(
        payload: URLNoteRequest,
        _session: AuthSession = Depends(require_session),
        idempotency_key: str | None = Depends(intake_idempotency_key),
    ) -> dict[str, object]:
        try:
            with intake_idempotency_scope(idempotency_key):
                intake = inbox.capture_url_note(
                    source_url=payload.source_url,
                    note=payload.note,
                    creator_hint=payload.creator_hint,
                    content_kind_hint=payload.content_kind_hint,
                )
        except IdempotencyReplay as replay:
            intake = replay.intake
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _intake_payload(intake)

    @app.post("/api/v1/inbox/files", status_code=201)
    def upload_file(
        file: UploadFile = File(...),
        source_url: str | None = Form(default=None, max_length=4096),
        note: str | None = Form(default=None, max_length=8192),
        creator_hint: str | None = Form(default=None, max_length=512),
        content_kind_hint: RegistryKey | None = Form(default=None),
        _session: AuthSession = Depends(require_session),
        idempotency_key: str | None = Depends(intake_idempotency_key),
    ) -> dict[str, object]:
        filename = file.filename or "upload.bin"
        if len(filename) > 1024:
            raise HTTPException(status_code=422, detail="filename is too long")
        if file.content_type is not None and len(file.content_type) > 255:
            raise HTTPException(status_code=422, detail="content type is too long")
        try:
            with intake_idempotency_scope(idempotency_key):
                intake = inbox.ingest_upload(
                    file.file,
                    filename=filename,
                    mime_type=file.content_type,
                    source_url=source_url,
                    note=note,
                    creator_hint=creator_hint,
                    content_kind_hint=content_kind_hint,
                )
        except IdempotencyReplay as replay:
            intake = replay.intake
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return _intake_payload(intake)

    @app.get("/api/v1/assets/{asset_id}/thumbnail")
    def thumbnail(
        asset_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> FileResponse:
        try:
            path = inbox.thumbnail_path(asset_id)
        except InboxError as exc:
            raise HTTPException(
                status_code=409,
                detail="thumbnail integrity check failed",
            ) from exc
        if path is None:
            raise HTTPException(status_code=404, detail="thumbnail not found")
        return FileResponse(path, media_type="image/jpeg", filename="thumbnail.jpg")

    return app