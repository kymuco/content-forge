"""Production YouTube Data API v3 publishing adapter for PR28."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import local
from typing import Any

from pydantic import Field, field_validator

from content_forge.core.models import FrozenModel

from .publishing import (
    ApprovedPublishRequest,
    PublishInvocationEvidence,
    PublishResult,
    PublishingExecutionError,
    PublishingPreflightError,
    PublishingProviderHealth,
    PublishingResponseError,
    PublishingUnavailableError,
    publish_idempotency_key,
    semantic_publish_request_digest,
)

_PROVIDER_ID = "youtube"
_PROVIDER_VERSION = "youtube_data_api_v3_pr28_v1"
_MAX_UPLOAD_BYTES = 256_000_000_000
_MAX_UPLOAD_SECONDS = 12 * 60 * 60
_LONG_UPLOAD_THRESHOLD_SECONDS = 15 * 60
_YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
_YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_OAUTH_SCOPES = (_YOUTUBE_UPLOAD_SCOPE, _YOUTUBE_READ_SCOPE)

CredentialsLoader = Callable[[Path], object]
ServiceFactory = Callable[[object], Any]
MediaUploadFactory = Callable[[Path], object]
Clock = Callable[[], datetime]


class YouTubePublishingConfig(FrozenModel):
    """Local-only YouTube runtime configuration; never part of publish identity."""

    token_path: str = Field(min_length=1, max_length=4096)
    channel_id: str = Field(min_length=3, max_length=128)
    max_retries: int = Field(default=5, ge=0, le=10)

    @field_validator("token_path")
    @classmethod
    def validate_token_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("token_path must contain non-whitespace content")
        path = Path(normalized).expanduser()
        if not path.is_absolute():
            raise ValueError("YouTube token_path must be absolute local runtime state")
        if path.name in {"", ".", ".."}:
            raise ValueError("YouTube token_path must identify a file")
        return str(path)

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("channel_id must contain non-whitespace content")
        if any(character.isspace() or ord(character) < 32 for character in normalized):
            raise ValueError("YouTube channel_id must be a canonical non-whitespace identifier")
        return normalized


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_token_file(path: Path) -> bool:
    """Reject symlinks and require owner-only permissions/ownership on POSIX."""

    if path.is_symlink() or not path.is_file():
        return False
    if os.name == "nt":
        return True
    try:
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
    except OSError:
        return False
    if mode & 0o077:
        return False
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        return False
    return True


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _load_credentials(token_path: Path) -> object:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except Exception as exc:  # pragma: no cover - optional dependency environment
        raise PublishingUnavailableError(
            "YouTube publishing dependencies are not installed"
        ) from exc

    if not _safe_token_file(token_path):
        raise PublishingUnavailableError(
            "YouTube OAuth token must be a regular private local file"
        )

    try:
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            scopes=list(YOUTUBE_OAUTH_SCOPES),
        )
    except Exception as exc:
        raise PublishingUnavailableError("YouTube OAuth token could not be loaded") from exc

    has_scopes = getattr(credentials, "has_scopes", None)
    if callable(has_scopes) and not has_scopes(YOUTUBE_OAUTH_SCOPES):
        raise PublishingUnavailableError("YouTube OAuth token lacks required scopes")

    if not getattr(credentials, "valid", False):
        if not getattr(credentials, "expired", False) or not getattr(
            credentials, "refresh_token", None
        ):
            raise PublishingUnavailableError("YouTube OAuth token is not refreshable")
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise PublishingUnavailableError("YouTube OAuth token refresh failed") from exc
        _write_refreshed_token(token_path, credentials.to_json())
    return credentials


def _write_refreshed_token(path: Path, payload: str) -> None:
    """Refresh an existing private token file without widening its permissions."""

    if path.is_symlink() or not path.parent.is_dir():
        raise PublishingUnavailableError("YouTube OAuth token path is unsafe")
    temp = path.with_name(
        f".{path.name}.refresh-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    )
    fd: int | None = None
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except Exception as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp.unlink()
        except OSError:
            pass
        raise PublishingUnavailableError(
            "YouTube OAuth token refresh could not be persisted"
        ) from exc


def _build_service(credentials: object) -> Any:
    try:
        from googleapiclient.discovery import build
    except Exception as exc:  # pragma: no cover - optional dependency environment
        raise PublishingUnavailableError(
            "google-api-python-client is not installed for YouTube publishing"
        ) from exc
    try:
        return build("youtube", "v3", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise PublishingUnavailableError("YouTube Data API client initialization failed") from exc


def _media_upload(path: Path) -> object:
    try:
        from googleapiclient.http import MediaFileUpload
    except Exception as exc:  # pragma: no cover - optional dependency environment
        raise PublishingPreflightError(
            "google-api-python-client media upload support is unavailable"
        ) from exc
    try:
        return MediaFileUpload(
            str(path),
            mimetype="video/mp4",
            chunksize=-1,
            resumable=True,
        )
    except Exception as exc:
        raise PublishingPreflightError(
            "YouTube resumable media upload could not be prepared"
        ) from exc


def _execute(request: Any, *, retries: int) -> dict[str, object]:
    try:
        payload = request.execute(num_retries=retries)
    except Exception as exc:
        raise PublishingExecutionError("YouTube Data API request failed") from exc
    if not isinstance(payload, dict):
        raise PublishingResponseError("YouTube Data API returned a non-object response")
    return payload


def _execute_preflight(request: Any, *, retries: int) -> dict[str, object]:
    """Execute a read-only API request while preserving retryable preflight semantics."""

    try:
        payload = request.execute(num_retries=retries)
    except Exception as exc:
        raise PublishingPreflightError("YouTube capability preflight request failed") from exc
    if not isinstance(payload, dict):
        raise PublishingPreflightError(
            "YouTube capability preflight returned a non-object response"
        )
    return payload


def _parse_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PublishingResponseError(f"YouTube {label} is missing")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PublishingResponseError(f"YouTube {label} is not ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublishingResponseError(f"YouTube {label} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _youtube_tag_budget(tags: tuple[str, ...]) -> int:
    total = max(0, len(tags) - 1)
    for tag in tags:
        total += len(tag)
        if any(character.isspace() for character in tag):
            total += 2
    return total


def _validate_youtube_metadata(request: ApprovedPublishRequest, *, now: datetime) -> None:
    metadata = request.request.metadata
    if len(metadata.title) > 100 or "<" in metadata.title or ">" in metadata.title:
        raise PublishingPreflightError(
            "YouTube title must be at most 100 characters and cannot contain angle brackets"
        )
    if (
        len(metadata.description.encode("utf-8")) > 5000
        or "<" in metadata.description
        or ">" in metadata.description
    ):
        raise PublishingPreflightError(
            "YouTube description must be at most 5000 UTF-8 bytes and cannot contain angle brackets"
        )
    if _youtube_tag_budget(metadata.tags) > 500:
        raise PublishingPreflightError("YouTube tags exceed the 500-character API budget")

    scheduled_for = metadata.scheduled_for
    if scheduled_for is None:
        return
    if metadata.visibility != "public":
        raise PublishingPreflightError(
            "YouTube scheduled publishing requires approved public visibility"
        )
    if scheduled_for.microsecond != 0:
        raise PublishingPreflightError(
            "YouTube scheduled publishing requires whole-second precision"
        )
    if scheduled_for <= now:
        raise PublishingPreflightError(
            "YouTube scheduled publishing time must be in the future"
        )


def _validate_upload_capability(
    service: Any,
    request: ApprovedPublishRequest,
    *,
    retries: int,
) -> None:
    duration = request.request.artifact.duration_seconds
    if duration > _MAX_UPLOAD_SECONDS:
        raise PublishingPreflightError("YouTube video exceeds the 12-hour upload limit")
    if duration <= _LONG_UPLOAD_THRESHOLD_SECONDS:
        return

    payload = _execute_preflight(
        service.channels().list(part="status", mine=True, maxResults=2),
        retries=retries,
    )
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise PublishingPreflightError(
            "YouTube long-upload capability could not be resolved exactly"
        )
    status = items[0].get("status")
    if not isinstance(status, dict) or status.get("longUploadsStatus") != "allowed":
        raise PublishingPreflightError(
            "YouTube channel is not currently allowed to upload videos longer than 15 minutes"
        )


def _youtube_body(request: ApprovedPublishRequest) -> dict[str, object]:
    metadata = request.request.metadata
    snippet: dict[str, object] = {
        "title": metadata.title,
        "description": metadata.description,
    }
    if metadata.tags:
        snippet["tags"] = list(metadata.tags)
    if metadata.scheduled_for is None:
        status: dict[str, object] = {"privacyStatus": metadata.visibility}
    else:
        status = {
            "privacyStatus": "private",
            "publishAt": metadata.scheduled_for.isoformat().replace("+00:00", "Z"),
        }
    return {"snippet": snippet, "status": status}


def _canonical_video_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PublishingResponseError("YouTube upload response lacks a canonical video ID")
    if len(value) > 128 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in value
    ):
        raise PublishingResponseError("YouTube upload response contains an invalid video ID")
    return value


@dataclass(frozen=True)
class _PreparedUpload:
    service: Any
    insert_request: Any
    request_sha256: str
    idempotency_key: str
    media_path: Path


class YouTubePublishingProvider:
    """YouTube Data API v3 adapter behind the PR27 publishing authority boundary."""

    def __init__(
        self,
        config: YouTubePublishingConfig,
        *,
        credentials_loader: CredentialsLoader | None = None,
        service_factory: ServiceFactory | None = None,
        media_upload_factory: MediaUploadFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self._credentials_loader = (
            _load_credentials if credentials_loader is None else credentials_loader
        )
        self._service_factory = _build_service if service_factory is None else service_factory
        self._media_upload_factory = (
            _media_upload if media_upload_factory is None else media_upload_factory
        )
        self._clock = _utc_now if clock is None else clock
        self._thread_state = local()

    def _clear_execution_state(self) -> None:
        for attribute in ("service", "prepared"):
            if hasattr(self._thread_state, attribute):
                delattr(self._thread_state, attribute)

    def health(self) -> PublishingProviderHealth:
        """Verify local credentials and pin one authenticated channel before execution."""

        self._clear_execution_state()
        try:
            credentials = self._credentials_loader(Path(self.config.token_path))
            service = self._service_factory(credentials)
            channels = _execute(
                service.channels().list(part="id", mine=True, maxResults=2),
                retries=self.config.max_retries,
            )
            items = channels.get("items")
            if not isinstance(items, list) or len(items) != 1:
                return PublishingProviderHealth(
                    provider_id=_PROVIDER_ID,
                    provider_version=_PROVIDER_VERSION,
                    available=False,
                    reason="YouTube authorization did not resolve exactly one channel",
                )
            channel = items[0]
            channel_id = channel.get("id") if isinstance(channel, dict) else None
            if channel_id != self.config.channel_id:
                return PublishingProviderHealth(
                    provider_id=_PROVIDER_ID,
                    provider_version=_PROVIDER_VERSION,
                    available=False,
                    reason="YouTube authenticated channel does not match configured destination",
                )
            self._thread_state.service = service
            return PublishingProviderHealth(
                provider_id=_PROVIDER_ID,
                provider_version=_PROVIDER_VERSION,
                available=True,
                reason=None,
            )
        except Exception:
            self._clear_execution_state()
            return PublishingProviderHealth(
                provider_id=_PROVIDER_ID,
                provider_version=_PROVIDER_VERSION,
                available=False,
                reason="YouTube publishing runtime is unavailable",
            )

    def preflight(
        self,
        request: ApprovedPublishRequest,
        *,
        media_path: Path,
        idempotency_key: str,
    ) -> None:
        """Finish safe validation/request construction before remote upload begins."""

        service = getattr(self._thread_state, "service", None)
        if service is None:
            raise PublishingPreflightError(
                "YouTube preflight requires a successful provider health check"
            )
        if hasattr(self._thread_state, "prepared"):
            del self._thread_state.prepared

        target = request.request.target
        if target.provider_id != _PROVIDER_ID:
            raise PublishingPreflightError("publish target is not the YouTube provider")
        if target.destination_id != self.config.channel_id:
            raise PublishingPreflightError(
                "publish target does not match the configured YouTube channel"
            )
        if idempotency_key != publish_idempotency_key(request.request):
            raise PublishingPreflightError("YouTube publish idempotency identity mismatch")
        try:
            size = media_path.stat().st_size
        except OSError as exc:
            raise PublishingPreflightError("YouTube media file is unavailable") from exc
        if size != request.request.artifact.bytes_written:
            raise PublishingPreflightError(
                "YouTube media file size differs from approved artifact evidence"
            )
        if size > _MAX_UPLOAD_BYTES:
            raise PublishingPreflightError("YouTube media file exceeds the 256 GB upload limit")

        _validate_upload_capability(service, request, retries=self.config.max_retries)

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PublishingPreflightError("YouTube provider clock must be timezone-aware")
        _validate_youtube_metadata(request, now=now.astimezone(timezone.utc))

        try:
            upload = self._media_upload_factory(media_path)
            insert_request = service.videos().insert(
                part="snippet,status",
                body=_youtube_body(request),
                media_body=upload,
            )
        except PublishingPreflightError:
            raise
        except Exception as exc:
            raise PublishingPreflightError(
                "YouTube upload request could not be prepared"
            ) from exc

        self._thread_state.prepared = _PreparedUpload(
            service=service,
            insert_request=insert_request,
            request_sha256=semantic_publish_request_digest(request.request),
            idempotency_key=idempotency_key,
            media_path=media_path,
        )
        del self._thread_state.service

    def publish(
        self,
        request: ApprovedPublishRequest,
        *,
        media_path: Path,
        idempotency_key: str,
    ) -> PublishResult:
        """Cross the remote boundary with a prebuilt resumable request, then verify it."""

        prepared = getattr(self._thread_state, "prepared", None)
        if hasattr(self._thread_state, "prepared"):
            del self._thread_state.prepared
        if not isinstance(prepared, _PreparedUpload):
            raise PublishingUnavailableError(
                "YouTube publish requires successful preflight on this execution thread"
            )
        expected_digest = semantic_publish_request_digest(request.request)
        if (
            prepared.request_sha256 != expected_digest
            or prepared.idempotency_key != idempotency_key
            or prepared.media_path != media_path
        ):
            raise PublishingResponseError(
                "YouTube prepared upload does not match the exact approved execution"
            )

        try:
            response: object = None
            while response is None:
                _progress, response = prepared.insert_request.next_chunk(
                    num_retries=self.config.max_retries
                )
        except Exception as exc:
            raise PublishingExecutionError("YouTube resumable upload failed") from exc

        if not isinstance(response, dict):
            raise PublishingResponseError("YouTube upload returned a non-object response")
        video_id = _canonical_video_id(response.get("id"))

        verified = _execute(
            prepared.service.videos().list(
                part="snippet,status",
                id=video_id,
                maxResults=1,
            ),
            retries=self.config.max_retries,
        )
        items = verified.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise PublishingResponseError("YouTube uploaded video could not be verified")
        video = items[0]
        if video.get("id") != video_id:
            raise PublishingResponseError("YouTube verification returned a different video ID")
        snippet = video.get("snippet")
        status = video.get("status")
        if not isinstance(snippet, dict) or not isinstance(status, dict):
            raise PublishingResponseError("YouTube verification response lacks snippet/status")
        if snippet.get("channelId") != self.config.channel_id:
            raise PublishingResponseError("YouTube video belongs to a different channel")

        metadata = request.request.metadata
        if metadata.scheduled_for is None:
            if status.get("privacyStatus") != metadata.visibility:
                raise PublishingResponseError(
                    "YouTube video privacy does not match approved visibility"
                )
            effective_at = _parse_datetime(snippet.get("publishedAt"), label="publishedAt")
            disposition = "published"
        else:
            if status.get("privacyStatus") != "private":
                raise PublishingResponseError(
                    "YouTube scheduled video is not private before publishAt"
                )
            remote_schedule = _parse_datetime(status.get("publishAt"), label="publishAt")
            if remote_schedule != metadata.scheduled_for:
                raise PublishingResponseError(
                    "YouTube publishAt does not match the approved schedule"
                )
            effective_at = metadata.scheduled_for
            disposition = "scheduled"

        return PublishResult(
            disposition=disposition,
            remote_id=video_id,
            remote_url=f"https://youtu.be/{video_id}",
            effective_at=effective_at,
            evidence=PublishInvocationEvidence(
                provider_id=_PROVIDER_ID,
                provider_version=_PROVIDER_VERSION,
                request_sha256=expected_digest,
                idempotency_key=idempotency_key,
                output_sha256=request.request.artifact.output_sha256,
                destination_id=request.request.target.destination_id,
            ),
        )


__all__ = [
    "YOUTUBE_OAUTH_SCOPES",
    "YouTubePublishingConfig",
    "YouTubePublishingProvider",
]
