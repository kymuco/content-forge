"""Authenticated PR26 production-library search and organization API."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from content_forge.application import AuthManager, AuthenticationError, AuthSession
from content_forge.core import EntityKind, RegistryKey, require_entity_id
from content_forge.storage import (
    LibrarySearchQuery,
    LibraryTag,
    LocalLibrary,
    MissingAssetError,
    StorageSchemaError,
)
from content_forge.web import static_path

from .app import _transport_is_secure

_PRODUCTION_LIBRARY_JSON_BODY_LIMIT = 64 * 1024


class LibraryTagReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: tuple[LibraryTag, ...] = Field(default=(), max_length=128)


class VirtualCollectionPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    query: LibrarySearchQuery


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


def _asset_path_id(value: str) -> str:
    try:
        return require_entity_id(value, EntityKind.ASSET)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid asset ID") from exc


def _collection_id(value: str) -> str:
    try:
        # Reuse the canonical RegistryKey validator via the Pydantic request contract.
        class _CollectionId(BaseModel):
            model_config = ConfigDict(extra="forbid")
            value: RegistryKey

        return _CollectionId(value=value).value
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid collection ID") from exc


def _asset_http_error(exc: MissingAssetError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def install_production_library_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
) -> None:
    """Install PR26 routes while keeping the additive index lazy until first use."""

    @app.middleware("http")
    async def pr26_production_library_transport_boundary(request: Request, call_next):
        route_path = _route_relative_path(request)
        if not (
            route_path == "/api/v1/production-library"
            or route_path.startswith("/api/v1/production-library/")
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
                return JSONResponse(status_code=415, content={"detail": "application/json is required"})
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return JSONResponse(
                    status_code=411,
                    content={"detail": "Content-Length is required for production library bodies"},
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if content_length > _PRODUCTION_LIBRARY_JSON_BODY_LIMIT:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "production library request body exceeds limit"},
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

    @app.get("/app/production-library.js", include_in_schema=False)
    def production_library_script() -> FileResponse:
        response = FileResponse(
            static_path("production-library.js"),
            media_type="text/javascript; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/api/v1/production-library/search")
    def search_library(
        query: LibrarySearchQuery,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            return {"items": [item.model_dump(mode="json") for item in library.index.search(query)]}
        except StorageSchemaError as exc:
            raise HTTPException(status_code=500, detail="production library schema unavailable") from exc

    @app.get("/api/v1/production-library/assets/{asset_id}/tags")
    def get_asset_tags(
        asset_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        asset_id = _asset_path_id(asset_id)
        try:
            return {
                "asset_id": asset_id,
                "tags": [tag.model_dump(mode="json") for tag in library.index.tags_for_asset(asset_id)],
            }
        except MissingAssetError as exc:
            raise _asset_http_error(exc) from exc

    @app.put("/api/v1/production-library/assets/{asset_id}/tags")
    def replace_asset_tags(
        asset_id: str,
        payload: LibraryTagReplacementRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        asset_id = _asset_path_id(asset_id)
        try:
            tags = library.index.set_tags(asset_id, payload.tags)
            return {"asset_id": asset_id, "tags": [tag.model_dump(mode="json") for tag in tags]}
        except MissingAssetError as exc:
            raise _asset_http_error(exc) from exc

    @app.get("/api/v1/production-library/assets/{asset_id}/reuse")
    def asset_reuse_history(
        asset_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        asset_id = _asset_path_id(asset_id)
        try:
            return {
                "asset_id": asset_id,
                "items": [
                    item.model_dump(mode="json") for item in library.index.reuse_history(asset_id)
                ],
            }
        except MissingAssetError as exc:
            raise _asset_http_error(exc) from exc

    @app.get("/api/v1/production-library/duplicates/{sha256}")
    def duplicate_lookup(
        sha256: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        try:
            match = library.index.duplicate_info(sha256)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"sha256": sha256.strip().lower(), "match": None if match is None else match.model_dump(mode="json")}

    @app.get("/api/v1/production-library/collections")
    def list_collections(
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        return {
            "items": [item.model_dump(mode="json") for item in library.index.list_collections()]
        }

    @app.put("/api/v1/production-library/collections/{collection_id}")
    def put_collection(
        collection_id: str,
        payload: VirtualCollectionPutRequest,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        collection_id = _collection_id(collection_id)
        item = library.index.put_collection(collection_id, payload.name, payload.query)
        return item.model_dump(mode="json")

    @app.get("/api/v1/production-library/collections/{collection_id}")
    def get_collection(
        collection_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        collection_id = _collection_id(collection_id)
        item = library.index.get_collection(collection_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"unknown virtual collection: {collection_id}")
        return item.model_dump(mode="json")

    @app.get("/api/v1/production-library/collections/{collection_id}/items")
    def collection_items(
        collection_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        collection_id = _collection_id(collection_id)
        try:
            items = library.index.search_collection(collection_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        return {"collection_id": collection_id, "items": [item.model_dump(mode="json") for item in items]}

    @app.delete("/api/v1/production-library/collections/{collection_id}")
    def delete_collection(
        collection_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, object]:
        collection_id = _collection_id(collection_id)
        if not library.index.delete_collection(collection_id):
            raise HTTPException(status_code=404, detail=f"unknown virtual collection: {collection_id}")
        return {"collection_id": collection_id, "deleted": True}


__all__ = [
    "LibraryTagReplacementRequest",
    "VirtualCollectionPutRequest",
    "install_production_library_routes",
]
