"""Authenticated PR21 Voice Cast registry, binding, and preview HTTP surface."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from content_forge.application import (
    AuthManager,
    AuthenticationError,
    AuthSession,
    LineTTSSettings,
    TTSError,
    TTSConflictError,
    TTSNotFoundError,
    TTSSynthesisError,
    TTSValidationError,
    VoiceCastConflictError,
    VoiceCastDefinition,
    VoiceCastError,
    VoiceCastNotFoundError,
    VoiceCastUnavailableError,
    VoiceCastValidationError,
    VoiceCastWorkflow,
)
from content_forge.application.dialogue_pr19_integrity import validated_dialogue_manifest
from content_forge.core import MediaType
from content_forge.providers.tts import TTSProvider
from content_forge.storage import LocalLibrary
from content_forge.web import static_path

from .app import _transport_is_secure

_VOICE_CAST_JSON_BODY_LIMIT = 128 * 1024


class VoiceCastBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cast_id: str
    cast_revision: int | None = None
    settings_override: LineTTSSettings | None = None


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


def _voice_cast_http_error(exc: VoiceCastError | TTSError) -> HTTPException:
    if isinstance(exc, (VoiceCastNotFoundError, TTSNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, VoiceCastUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (VoiceCastConflictError, TTSConflictError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (VoiceCastValidationError, TTSValidationError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, TTSSynthesisError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail="voice cast workflow failed")


def _project_payload(workflow: VoiceCastWorkflow, project_id: str) -> dict[str, object]:
    project, _ = workflow._snapshot(project_id)
    dialogue = validated_dialogue_manifest(project)
    cast_manifest = workflow._validated_manifest(project, dialogue)
    return {
        "project_id": project.project_id,
        "project_state": project.state.value,
        "characters": [item.model_dump(mode="json") for item in dialogue.characters],
        "bindings": [item.model_dump(mode="json") for item in cast_manifest.bindings],
    }


def install_voice_cast_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
    tts_provider: TTSProvider | None = None,
) -> VoiceCastWorkflow:
    """Install PR21 routes with auth/body gates before JSON parsing."""

    workflow = VoiceCastWorkflow(library, tts_provider)
    app.state.voice_cast = workflow
    app.state.tts_provider = tts_provider

    @app.middleware("http")
    async def pr21_voice_cast_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not (
            route_path == "/api/v1/voice-cast"
            or route_path.startswith("/api/v1/voice-cast/")
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
                    content={"detail": "Content-Length is required for voice cast bodies"},
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length > _VOICE_CAST_JSON_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "voice cast request body exceeds limit"},
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

    @app.get("/app/voice-cast.js", include_in_schema=False)
    def voice_cast_script() -> FileResponse:
        response = FileResponse(
            static_path("voice-cast.js"),
            media_type="text/javascript; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/voice-cast")
    def list_cast(
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return {
                "items": [
                    item.model_dump(mode="json") for item in workflow.registry.list_latest()
                ]
            }
        except VoiceCastError as exc:
            raise _voice_cast_http_error(exc) from exc

    @app.post("/api/v1/voice-cast", status_code=status.HTTP_201_CREATED)
    def put_cast(
        definition: VoiceCastDefinition,
        response: Response,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            previous = workflow.registry.repository.get(definition.cast_id)
            revision = workflow.registry.put(definition)
            if previous is not None and previous == revision:
                response.status_code = status.HTTP_200_OK
            return revision.model_dump(mode="json")
        except VoiceCastError as exc:
            raise _voice_cast_http_error(exc) from exc

    @app.get("/api/v1/voice-cast/projects/{project_id}")
    def project_cast(
        project_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return _project_payload(workflow, project_id)
        except (VoiceCastError, TTSError) as exc:
            raise _voice_cast_http_error(exc) from exc
        except Exception as exc:
            # accepted PR19 integrity failures are conflicts at this boundary
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put(
        "/api/v1/voice-cast/projects/{project_id}/characters/{character_id}"
    )
    def bind_character(
        project_id: str,
        character_id: str,
        payload: VoiceCastBindRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            workflow.bind_character(
                project_id,
                character_id,
                payload.cast_id,
                cast_revision=payload.cast_revision,
                settings_override=payload.settings_override,
            )
            return _project_payload(workflow, project_id)
        except (VoiceCastError, TTSError) as exc:
            raise _voice_cast_http_error(exc) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete(
        "/api/v1/voice-cast/projects/{project_id}/characters/{character_id}"
    )
    def unbind_character(
        project_id: str,
        character_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            workflow.unbind_character(project_id, character_id)
            return _project_payload(workflow, project_id)
        except (VoiceCastError, TTSError) as exc:
            raise _voice_cast_http_error(exc) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/v1/voice-cast/projects/{project_id}/characters/{character_id}/preview"
    )
    def preview_character(
        project_id: str,
        character_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> FileResponse:
        try:
            resolved, synthesized = workflow.preview_character(project_id, character_id)
            asset = library.database.get_asset(synthesized.asset_id)
            if asset is None or asset.media_type is not MediaType.AUDIO:
                raise VoiceCastConflictError("voice preview audio asset is unavailable")
            if not library.assets.verify(asset):
                raise VoiceCastConflictError("voice preview audio failed content verification")
            path = library.assets.resolve(asset)
        except (VoiceCastError, TTSError) as exc:
            raise _voice_cast_http_error(exc) from exc
        response = FileResponse(path, media_type="audio/wav", filename="voice-preview.wav")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Content-Forge-Cast"] = (
            f"{resolved.cast_id}@{resolved.cast_revision}"
        )
        response.headers["X-Content-Forge-Audio-SHA256"] = synthesized.audio_sha256
        return response

    return workflow


__all__ = ["VoiceCastBindRequest", "install_voice_cast_routes"]
