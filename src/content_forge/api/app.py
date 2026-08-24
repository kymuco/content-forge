"""Authenticated local FastAPI transport for Content Forge application services."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from content_forge.application import (
    ApplicationRepository,
    AuthManager,
    AuthenticationError,
    AuthSession,
    InboxIntake,
    InboxService,
    UploadTooLargeError,
)
from content_forge.core import RegistryKey
from content_forge.storage import LocalLibrary


class PairExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str
    code: str = Field(min_length=8, max_length=8)
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


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    host = request.client.host
    if host == "testclient":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def create_app(
    *,
    root: str | Path | None = None,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024,
) -> FastAPI:
    library = LocalLibrary(root)
    repository = ApplicationRepository(library.database).initialize()
    auth = AuthManager(repository)
    inbox = InboxService(
        library,
        repository,
        ffprobe_path=ffprobe_path,
        ffmpeg_path=ffmpeg_path,
        max_upload_bytes=max_upload_bytes,
    )

    app = FastAPI(
        title="Content Forge Local API",
        version="0.0.1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.library = library
    app.state.application_repository = repository
    app.state.auth = auth
    app.state.inbox = inbox

    def bearer_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="bearer token required")
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="bearer token required")
        return token

    def require_session(token: Annotated[str, Depends(bearer_token)]) -> AuthSession:
        try:
            return auth.authenticate(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "service": "content-forge", "api_version": "v1"}

    @app.post("/api/v1/pairing/challenges", status_code=201)
    def create_pairing_challenge(request: Request) -> dict[str, object]:
        if not _is_loopback(request):
            raise HTTPException(
                status_code=403,
                detail="pairing challenges may only be created from loopback",
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
        token: Annotated[str, Depends(bearer_token)],
        _session: Annotated[AuthSession, Depends(require_session)],
    ) -> None:
        try:
            auth.revoke(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/v1/inbox")
    def list_inbox(
        _session: Annotated[AuthSession, Depends(require_session)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        items = inbox.list_intakes(limit=limit)
        return {"items": [_intake_payload(item) for item in items]}

    @app.get("/api/v1/inbox/{intake_id}")
    def get_inbox_item(
        intake_id: str,
        _session: Annotated[AuthSession, Depends(require_session)],
    ) -> dict[str, object]:
        intake = inbox.get_intake(intake_id)
        if intake is None:
            raise HTTPException(status_code=404, detail="intake not found")
        return _intake_payload(intake)

    @app.post("/api/v1/inbox/url-note", status_code=201)
    def capture_url_note(
        payload: URLNoteRequest,
        _session: Annotated[AuthSession, Depends(require_session)],
    ) -> dict[str, object]:
        intake = inbox.capture_url_note(
            source_url=payload.source_url,
            note=payload.note,
            creator_hint=payload.creator_hint,
            content_kind_hint=payload.content_kind_hint,
        )
        return _intake_payload(intake)

    @app.post("/api/v1/inbox/files", status_code=201)
    def upload_file(
        _session: Annotated[AuthSession, Depends(require_session)],
        file: Annotated[UploadFile, File()],
        source_url: Annotated[str | None, Form(max_length=4096)] = None,
        note: Annotated[str | None, Form(max_length=8192)] = None,
        creator_hint: Annotated[str | None, Form(max_length=512)] = None,
        content_kind_hint: Annotated[RegistryKey | None, Form()] = None,
    ) -> dict[str, object]:
        filename = file.filename or "upload.bin"
        if len(filename) > 1024:
            raise HTTPException(status_code=422, detail="filename is too long")
        if file.content_type is not None and len(file.content_type) > 255:
            raise HTTPException(status_code=422, detail="content type is too long")
        try:
            intake = inbox.ingest_upload(
                file.file,
                filename=filename,
                mime_type=file.content_type,
                source_url=source_url,
                note=note,
                creator_hint=creator_hint,
                content_kind_hint=content_kind_hint,
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return _intake_payload(intake)

    @app.get("/api/v1/assets/{asset_id}/thumbnail")
    def thumbnail(
        asset_id: str,
        _session: Annotated[AuthSession, Depends(require_session)],
    ) -> FileResponse:
        path = inbox.thumbnail_path(asset_id)
        if path is None:
            raise HTTPException(status_code=404, detail="thumbnail not found")
        return FileResponse(path, media_type="image/jpeg", filename="thumbnail.jpg")

    return app
