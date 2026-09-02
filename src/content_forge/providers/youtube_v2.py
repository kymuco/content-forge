"""PR29 publication-declaration extension for the proven PR28 YouTube adapter."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .publishing import (
    ApprovedPublishRequest,
    PublishResult,
    PublishTarget,
    PublishingPreflightError,
    PublishingProviderHealth,
    PublishingResponseError,
)
from .youtube import (
    YouTubePublishingConfig,
    YouTubePublishingProvider as _PR28YouTubePublishingProvider,
    _NOTIFY_SUBSCRIBERS,
    _PreparedUpload,
    _verified_video,
    _youtube_body,
)

_PROVIDER_VERSION = "youtube_data_api_v3_pr29_v2:category=22:notify=0:decl=2"
_V2 = "pr29_publish_contract_v2"


def _youtube_body_with_declarations(
    request: ApprovedPublishRequest,
) -> dict[str, object]:
    """Keep v1 remote semantics unchanged; add declarations only for v2 requests."""

    body = _youtube_body(request)
    if request.request.contract_version != _V2:
        return body
    declarations = request.request.declarations
    if declarations is None:
        raise PublishingPreflightError(
            "YouTube v2 publishing requires approved publication declarations"
        )
    status = body.get("status")
    if not isinstance(status, dict):
        raise PublishingPreflightError("YouTube v2 upload body lacks status metadata")
    status["selfDeclaredMadeForKids"] = declarations.child_directed
    status["containsSyntheticMedia"] = (
        declarations.contains_realistic_altered_or_synthetic_media
    )
    return body


def _verify_declarations(
    status: dict[str, object],
    request: ApprovedPublishRequest,
) -> None:
    if request.request.contract_version != _V2:
        return
    declarations = request.request.declarations
    if declarations is None:
        raise PublishingResponseError(
            "YouTube v2 result cannot be verified without approved declarations"
        )
    if status.get("selfDeclaredMadeForKids") is not declarations.child_directed:
        raise PublishingResponseError(
            "YouTube made-for-kids declaration does not match approved request"
        )
    expected_synthetic = declarations.contains_realistic_altered_or_synthetic_media
    if status.get("containsSyntheticMedia") is not expected_synthetic:
        raise PublishingResponseError(
            "YouTube altered/synthetic-media declaration does not match approved request"
        )


class YouTubePublishingProvider(_PR28YouTubePublishingProvider):
    """PR28 runtime plus exact PR29 publication declarations."""

    def configured_target(self) -> PublishTarget:
        """Expose only the credential-free configured channel identity to phone UX."""

        return PublishTarget(provider_id="youtube", destination_id=self.config.channel_id)

    def health(self) -> PublishingProviderHealth:
        health = super().health()
        return health.model_copy(update={"provider_version": _PROVIDER_VERSION})

    def preflight(
        self,
        request: ApprovedPublishRequest,
        *,
        media_path: Path,
        idempotency_key: str,
    ) -> None:
        # Preserve the proven PR28 validation/snapshot path. For v2, replace only
        # the locally constructed insert request before the durable running boundary.
        super().preflight(
            request,
            media_path=media_path,
            idempotency_key=idempotency_key,
        )
        if request.request.contract_version != _V2:
            return

        prepared = getattr(self._thread_state, "prepared", None)
        if not isinstance(prepared, _PreparedUpload):
            self._clear_execution_state()
            raise PublishingPreflightError(
                "YouTube v2 preflight did not retain authenticated upload state"
            )
        try:
            prepared.media_snapshot.seek(0)
            upload = self._media_upload_factory(prepared.media_snapshot)
            insert_request = prepared.service.videos().insert(
                part="snippet,status",
                body=_youtube_body_with_declarations(request),
                media_body=upload,
                notifySubscribers=_NOTIFY_SUBSCRIBERS,
            )
        except PublishingPreflightError:
            self._clear_execution_state()
            raise
        except Exception as exc:
            self._clear_execution_state()
            raise PublishingPreflightError(
                "YouTube v2 upload request could not be prepared"
            ) from exc

        self._thread_state.prepared = replace(
            prepared,
            insert_request=insert_request,
        )

    def publish(
        self,
        request: ApprovedPublishRequest,
        *,
        media_path: Path,
        idempotency_key: str,
    ) -> PublishResult:
        prepared = getattr(self._thread_state, "prepared", None)
        service = prepared.service if isinstance(prepared, _PreparedUpload) else None

        result = super().publish(
            request,
            media_path=media_path,
            idempotency_key=idempotency_key,
        )

        if request.request.contract_version == _V2:
            if service is None:
                raise PublishingResponseError(
                    "YouTube v2 verification lost the authenticated service context"
                )
            video = _verified_video(
                service,
                result.remote_id,
                retries=self.config.max_retries,
            )
            status = video.get("status")
            if not isinstance(status, dict):
                raise PublishingResponseError(
                    "YouTube v2 verification response lacks status"
                )
            _verify_declarations(status, request)

        evidence = result.evidence.model_copy(
            update={
                "contract_version": request.request.contract_version,
                "provider_version": _PROVIDER_VERSION,
            }
        )
        return result.model_copy(update={"evidence": evidence})


__all__ = [
    "YouTubePublishingConfig",
    "YouTubePublishingProvider",
]
