"""Read-only PR34 project-to-publishing projection over the PR27 ledger."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from content_forge.application import AuthManager, AuthenticationError, AuthSession
from content_forge.core import EntityKind, require_entity_id
from content_forge.providers import PublishRequest, PublishTarget, PublishingProvider
from content_forge.storage import LocalLibrary, StorageConflictError


def _authorization_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise AuthenticationError("bearer token required")
    token = value[7:].strip()
    if not token:
        raise AuthenticationError("bearer token required")
    return token


def _configured_target(provider: PublishingProvider | None) -> PublishTarget | None:
    """Project safe credential-free target identity when a provider exposes one."""

    if provider is None:
        return None
    resolver = getattr(provider, "configured_target", None)
    if not callable(resolver):
        return None
    try:
        return PublishTarget.model_validate(resolver())
    except Exception:
        # Provider-local configuration errors must never leak paths, credentials, or
        # exception text through this convenience projection.
        return None


def _attempt_payload(library: LocalLibrary, attempt_id: str) -> dict[str, object]:
    repository = library.publishing
    attempt = repository.get_attempt(attempt_id)
    if attempt is None:
        raise StorageConflictError("project publishing projection references missing attempt")
    approved = repository.approved_request(attempt_id)
    operation = repository.get_operation(attempt.request_sha256)
    if operation is None:
        raise StorageConflictError("project publishing projection references missing operation")
    return {
        "attempt": attempt.model_dump(mode="json"),
        "request": approved.request.model_dump(mode="json"),
        "request_sha256": attempt.request_sha256,
        "idempotency_key": operation.idempotency_key,
    }


def _project_attempt_ids(
    library: LocalLibrary,
    project_id: str,
    *,
    limit: int,
) -> tuple[str, ...]:
    """Return newest attempts for one project, applying limit after exact filtering."""

    # Accessing the repository initializes the additive publishing schema before the
    # read-only join below. No ledger row is created or mutated by this projection.
    library.publishing
    with library.database.connection() as connection:
        rows = connection.execute(
            """
            SELECT a.attempt_id, o.request_json
            FROM publish_attempts AS a
            JOIN publish_operations AS o
              ON o.request_sha256 = a.request_sha256
            ORDER BY a.created_at DESC, a.attempt_number DESC, a.attempt_id DESC
            """
        ).fetchall()

    selected: list[str] = []
    for row in rows:
        try:
            request = PublishRequest.model_validate_json(str(row["request_json"]))
        except Exception as exc:
            raise StorageConflictError("stored publish operation is invalid") from exc
        if request.artifact.project_id != project_id:
            continue
        selected.append(str(row["attempt_id"]))
        if len(selected) >= limit:
            break
    return tuple(selected)


def install_project_publishing_routes(
    app: FastAPI,
    *,
    auth: AuthManager,
    library: LocalLibrary,
    provider: PublishingProvider | None,
) -> None:
    """Install PR34 read projection without creating a second publish authority."""

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

    @app.get("/api/v1/publishing/projects/{project_id}")
    def project_publishing_context(
        project_id: str,
        _session: AuthSession = Depends(require_session),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, object]:
        try:
            project_id = require_entity_id(project_id, EntityKind.PROJECT)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        target = _configured_target(provider)
        try:
            attempt_ids = _project_attempt_ids(library, project_id, limit=limit)
            items = [_attempt_payload(library, attempt_id) for attempt_id in attempt_ids]
        except StorageConflictError as exc:
            raise HTTPException(
                status_code=500,
                detail="project publishing history is inconsistent",
            ) from exc

        return {
            "project_id": project_id,
            "provider_configured": provider is not None,
            "configured_target": (
                None if target is None else target.model_dump(mode="json")
            ),
            "preferred_contract_version": "pr29_publish_contract_v2",
            "items": items,
        }


__all__ = ["install_project_publishing_routes"]
