"""Authenticated server-side preparation for the complete PR10 eligible Project set."""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from content_forge.application import (
    AuthManager,
    AuthenticationError,
    ReviewService,
)


def _authorization_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise AuthenticationError("bearer token required")
    token = value[7:].strip()
    if not token:
        raise AuthenticationError("bearer token required")
    return token


def install_review_prepare_route(
    app: FastAPI,
    *,
    auth: AuthManager,
    review: ReviewService,
) -> None:
    """Install the bodyless authenticated bulk-preparation operation."""

    @app.post("/api/v1/review-prepare")
    def prepare_review_projects(
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        try:
            token = _authorization_token(authorization)
            auth.authenticate(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return review.prepare_inbox_projects()


__all__ = ["install_review_prepare_route"]
