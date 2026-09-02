"""PR27/PR29 authenticated export-to-publish orchestration."""

from __future__ import annotations

from pathlib import Path

from content_forge.orchestration import RenderArtifactManifest, RenderOrchestrator
from content_forge.providers import (
    ApprovedPublishRequest,
    PublishContractVersion,
    PublishDeclarations,
    PublishMetadata,
    PublishRequest,
    PublishTarget,
    PublishingExecutionError,
    PublishingPreflightProvider,
    PublishingProvider,
    PublishingResponseError,
    PublishingUnavailableError,
    approve_publish_request,
    publish_artifact_ref,
    publish_idempotency_key,
    semantic_publish_request_digest,
    validate_publish_result,
)
from content_forge.providers.publishing import _PublishingPreflightCleanupProvider
from content_forge.storage import LocalLibrary, PublishAttemptRecord, StorageConflictError


class PublishOrchestrationError(RuntimeError):
    """Base class for local publish-handoff failures."""


class PublishArtifactError(PublishOrchestrationError):
    """The approved publish request no longer matches authenticated final-render evidence."""


class PublishAttemptError(PublishOrchestrationError):
    """The requested durable publish attempt cannot enter remote execution."""


class PublishOutcomeUnknownError(PublishOrchestrationError):
    """Remote publishing began but no authenticated durable outcome is known."""


def _discard_preflight_state(provider: PublishingProvider) -> None:
    """Best-effort release of provider-local state while the remote boundary is still closed."""

    if not isinstance(provider, _PublishingPreflightCleanupProvider):
        return
    try:
        provider._clear_execution_state()
    except Exception:
        pass


class PublishingService:
    """Prepare and optionally execute publishing without weakening render authority."""

    def __init__(
        self,
        library: LocalLibrary,
        provider: PublishingProvider | None = None,
        *,
        render_orchestrator: RenderOrchestrator | None = None,
        ffprobe_path: str = "ffprobe",
        probe_timeout: float = 20.0,
    ) -> None:
        self.library = library
        self.provider = provider
        self.render_orchestrator = (
            RenderOrchestrator(library) if render_orchestrator is None else render_orchestrator
        )
        self.ffprobe_path = ffprobe_path
        self.probe_timeout = probe_timeout

    def _artifact_by_job_id(self, render_job_id: str) -> RenderArtifactManifest:
        try:
            artifact = self.render_orchestrator.load_artifact(
                render_job_id,
                ffprobe_path=self.ffprobe_path,
                probe_timeout=self.probe_timeout,
            )
        except Exception as exc:
            raise PublishArtifactError("failed to authenticate final render artifact") from exc
        if artifact is None:
            raise PublishArtifactError("render job has no authenticated successful artifact")
        try:
            publish_artifact_ref(artifact)
        except ValueError as exc:
            raise PublishArtifactError("render job is not a final artifact") from exc
        return artifact

    def candidate(
        self,
        render_job_id: str,
        *,
        target: PublishTarget,
        metadata: PublishMetadata,
        contract_version: PublishContractVersion = "pr27_publish_contract_v1",
        declarations: PublishDeclarations | None = None,
    ) -> PublishRequest:
        """Build a credential-free candidate from one authenticated final render."""

        artifact = self._artifact_by_job_id(render_job_id)
        return PublishRequest(
            contract_version=contract_version,
            artifact=publish_artifact_ref(artifact),
            target=target,
            metadata=metadata,
            declarations=declarations,
        )

    def _authenticated_artifact(self, approved: ApprovedPublishRequest) -> RenderArtifactManifest:
        expected = approved.request.artifact
        artifact = self._artifact_by_job_id(expected.render_job_id)
        actual = publish_artifact_ref(artifact)
        if actual != expected:
            raise PublishArtifactError("authenticated final artifact differs from approved publish input")
        return artifact

    def _media_path(self, artifact: RenderArtifactManifest) -> Path:
        path = self.library.paths.root / artifact.output_storage_key
        if not path.is_file():
            raise PublishArtifactError("authenticated final artifact path is missing")
        return path

    def approve(
        self,
        request: PublishRequest,
        *,
        confirm_request_sha256: str,
        note: str | None = None,
    ) -> PublishAttemptRecord:
        """Persist explicit approval only when the client confirms the exact request digest."""

        expected = semantic_publish_request_digest(request)
        if confirm_request_sha256 != expected:
            raise PublishAttemptError("publish approval digest does not match exact request")
        approved = approve_publish_request(request, note=note)
        return self.prepare(approved)

    def prepare(self, approved: ApprovedPublishRequest) -> PublishAttemptRecord:
        """Persist exact human approval without beginning remote execution."""

        self._authenticated_artifact(approved)
        existing = self.library.publishing.attempts(approved.approval.request_sha256)
        for attempt in reversed(existing):
            if attempt.state in {"prepared", "running", "succeeded"}:
                return attempt
            if attempt.state == "outcome_unknown":
                raise PublishOutcomeUnknownError(
                    "remote publish outcome is unknown; automatic retry is blocked"
                )
        return self.library.publishing.prepare_attempt(approved)

    def execute_prepared(self, attempt_id: str) -> PublishAttemptRecord:
        """Execute one durable approved attempt, retry-safe by attempt identity."""

        attempt = self.library.publishing.get_attempt(attempt_id)
        if attempt is None:
            raise PublishAttemptError("publish attempt not found")
        if attempt.state == "succeeded":
            return attempt
        if attempt.state == "outcome_unknown":
            raise PublishOutcomeUnknownError(
                "remote publish outcome is unknown; automatic retry is blocked"
            )
        if attempt.state != "prepared":
            raise PublishAttemptError(
                f"publish attempt is {attempt.state}; expected prepared"
            )
        provider = self.provider
        if provider is None:
            raise PublishingUnavailableError("publishing provider is not configured")

        approved = self.library.publishing.approved_request(attempt_id)
        artifact = self._authenticated_artifact(approved)
        media_path = self._media_path(artifact)

        try:
            health = provider.health()
        except Exception as exc:
            self.library.publishing.mark_failed(
                attempt.attempt_id,
                code="provider_health_failed",
                message="publishing provider health check failed before remote execution",
            )
            raise PublishingExecutionError("publishing provider health check failed") from exc

        if not health.available:
            self.library.publishing.mark_failed(
                attempt.attempt_id,
                code="provider_unavailable",
                message="publishing provider was unavailable before remote execution",
            )
            raise PublishingUnavailableError("publishing provider is unavailable")
        if health.provider_id != approved.request.target.provider_id:
            self.library.publishing.mark_failed(
                attempt.attempt_id,
                code="provider_identity_mismatch",
                message="publish target provider did not match provider health identity",
            )
            raise PublishingResponseError(
                "publish target provider does not match provider health identity"
            )

        idempotency_key = publish_idempotency_key(approved.request)
        preflight_completed = False
        if isinstance(provider, PublishingPreflightProvider):
            try:
                provider.preflight(
                    approved,
                    media_path=media_path,
                    idempotency_key=idempotency_key,
                )
                preflight_completed = True
            except Exception as exc:
                self.library.publishing.mark_failed(
                    attempt.attempt_id,
                    code="provider_preflight_failed",
                    message="publishing provider rejected the approved request before remote execution",
                )
                raise PublishAttemptError(
                    "publishing provider preflight rejected approved request"
                ) from exc

        try:
            running = self.library.publishing.mark_running(attempt.attempt_id, health)
        except Exception:
            if preflight_completed:
                _discard_preflight_state(provider)
            raise

        pinned_health = running.provider_health
        if pinned_health is None:
            if preflight_completed:
                _discard_preflight_state(provider)
            raise PublishOrchestrationError("running publish attempt lacks provider identity evidence")
        try:
            result = provider.publish(
                approved,
                media_path=media_path,
                idempotency_key=idempotency_key,
            )
            validate_publish_result(approved, pinned_health, result)
            return self.library.publishing.mark_succeeded(running.attempt_id, result)
        except Exception as exc:
            try:
                self.library.publishing.mark_outcome_unknown(
                    running.attempt_id,
                    code="remote_outcome_unknown",
                    message="remote publish execution began but no authenticated outcome was recorded",
                )
            except StorageConflictError as ledger_exc:
                raise PublishOrchestrationError(
                    "failed to persist unknown remote publish outcome"
                ) from ledger_exc
            raise PublishOutcomeUnknownError(
                "remote publish outcome is unknown; automatic retry is blocked"
            ) from exc

    def publish(self, approved: ApprovedPublishRequest) -> PublishAttemptRecord:
        """Convenience path for trusted callers that intentionally approve and execute now."""

        attempt = self.prepare(approved)
        return self.execute_prepared(attempt.attempt_id)

    def reconcile_interrupted(self) -> int:
        """Convert abandoned running attempts into retry-blocking unknown outcomes."""

        return self.library.publishing.reconcile_interrupted()


__all__ = [
    "PublishArtifactError",
    "PublishAttemptError",
    "PublishOrchestrationError",
    "PublishOutcomeUnknownError",
    "PublishingService",
]
