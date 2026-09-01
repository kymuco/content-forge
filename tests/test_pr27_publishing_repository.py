from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_forge.core import EntityKind, new_entity_id
from content_forge.orchestration import RenderArtifactManifest
from content_forge.providers import (
    PublishInvocationEvidence,
    PublishMetadata,
    PublishRequest,
    PublishResult,
    PublishTarget,
    PublishingProviderHealth,
    approve_publish_request,
    publish_artifact_ref,
    publish_idempotency_key,
    semantic_publish_request_digest,
)
from content_forge.storage import LibraryDatabase, StorageConflictError, StorageSchemaError
from content_forge.storage.publishing import PublishingRepository


def _approved(*, title: str = "Approved upload"):
    artifact = RenderArtifactManifest(
        job_id=new_entity_id(EntityKind.JOB),
        project_id=new_entity_id(EntityKind.PROJECT),
        purpose="final",
        profile_id="youtube_shorts_1080p",
        render_plan_digest="1" * 64,
        command_manifest_digest="2" * 64,
        command_manifest_storage_key="commands/final.json",
        output_sha256="3" * 64,
        output_storage_key="renders/final.mp4",
        manifest_storage_key="renders/final.json",
        video_encoder="libx264",
        ffmpeg_version="fixture",
        bytes_written=4096,
        elapsed_seconds=1.0,
        width=1080,
        height=1920,
        duration_seconds=9.0,
        fps=30.0,
        has_audio=True,
    )
    request = PublishRequest(
        artifact=publish_artifact_ref(artifact),
        target=PublishTarget(provider_id="fixture", destination_id="channel-main"),
        metadata=PublishMetadata(title=title, visibility="private"),
    )
    return approve_publish_request(
        request,
        approved_at=datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc),
    )


def _health(*, version: str = "1") -> PublishingProviderHealth:
    return PublishingProviderHealth(
        provider_id="fixture",
        provider_version=version,
        available=True,
    )


def _result(approved, health: PublishingProviderHealth, **updates) -> PublishResult:
    evidence = PublishInvocationEvidence(
        provider_id=health.provider_id,
        provider_version=health.provider_version,
        request_sha256=semantic_publish_request_digest(approved.request),
        idempotency_key=publish_idempotency_key(approved.request),
        output_sha256=approved.request.artifact.output_sha256,
        destination_id=approved.request.target.destination_id,
    ).model_copy(update=updates)
    return PublishResult(
        disposition="published",
        remote_id="remote-1",
        remote_url="https://example.invalid/watch/remote-1",
        effective_at=datetime(2026, 9, 2, 13, 5, tzinfo=timezone.utc),
        evidence=evidence,
    )


def _repository(tmp_path) -> PublishingRepository:
    database = LibraryDatabase(tmp_path / "library.sqlite3").initialize()
    return PublishingRepository(database).initialize()


def test_pr27_publishing_schema_is_additive_idempotent_and_future_fail_closed(tmp_path) -> None:
    database = LibraryDatabase(tmp_path / "library.sqlite3").initialize()
    with database.connection() as connection:
        base_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

    PublishingRepository(database).initialize()
    PublishingRepository(database).initialize()
    with database.connection() as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == base_version
        row = connection.execute(
            "SELECT version FROM application_schema WHERE component = 'publishing'"
        ).fetchone()
        assert row is not None and int(row["version"]) == 1

    with database.transaction() as connection:
        connection.execute(
            "UPDATE application_schema SET version = 2 WHERE component = 'publishing'"
        )
    with pytest.raises(StorageSchemaError, match="newer than supported"):
        PublishingRepository(database).initialize()


def test_pr27_publish_operation_and_success_lifecycle(tmp_path) -> None:
    repository = _repository(tmp_path)
    approved = _approved()
    operation = repository.ensure_operation(approved)
    assert operation.request_sha256 == approved.approval.request_sha256
    assert operation.idempotency_key == publish_idempotency_key(approved.request)
    assert repository.ensure_operation(approved) == operation

    prepared = repository.prepare_attempt(approved)
    assert prepared.attempt_id.startswith("cf_publish_")
    assert prepared.attempt_number == 1
    assert prepared.state == "prepared"
    assert prepared.provider_health is None

    health = _health(version="provider-v1")
    running = repository.mark_running(prepared.attempt_id, health)
    assert running.state == "running"
    assert running.provider_health == health
    assert repository.approved_request(prepared.attempt_id) == approved

    succeeded = repository.mark_succeeded(prepared.attempt_id, _result(approved, health))
    assert succeeded.state == "succeeded"
    assert succeeded.result is not None
    assert repository.get_attempt(prepared.attempt_id) == succeeded

    with pytest.raises(StorageConflictError, match="already succeeded"):
        repository.prepare_attempt(approved)


def test_pr27_known_failure_allows_retry_with_same_remote_idempotency_key(tmp_path) -> None:
    repository = _repository(tmp_path)
    approved = _approved()
    first = repository.prepare_attempt(approved)
    failed = repository.mark_failed(first.attempt_id, code="preflight_failed", message="no remote call made")
    assert failed.state == "failed"
    assert failed.provider_health is None

    second = repository.prepare_attempt(approved)
    assert second.attempt_number == 2
    assert second.attempt_id != first.attempt_id
    operation = repository.get_operation(second.request_sha256)
    assert operation is not None
    assert operation.idempotency_key == publish_idempotency_key(approved.request)


def test_pr27_unknown_remote_outcome_blocks_retry_and_restart_reconciliation(tmp_path) -> None:
    repository = _repository(tmp_path)
    approved = _approved()
    first = repository.prepare_attempt(approved)
    running = repository.mark_running(first.attempt_id, _health())
    assert running.state == "running"

    assert repository.reconcile_running_as_unknown() == 1
    unknown = repository.get_attempt(first.attempt_id)
    assert unknown is not None
    assert unknown.state == "outcome_unknown"
    assert unknown.provider_health == _health()
    assert unknown.error_code == "runtime_interrupted"

    with pytest.raises(StorageConflictError, match="unresolved remote outcome"):
        repository.prepare_attempt(approved)
    assert repository.reconcile_running_as_unknown() == 0


def test_pr27_result_evidence_is_checked_against_pinned_provider_health(tmp_path) -> None:
    repository = _repository(tmp_path)
    approved = _approved()
    attempt = repository.prepare_attempt(approved)
    health = _health(version="pinned-v1")
    repository.mark_running(attempt.attempt_id, health)

    with pytest.raises(StorageConflictError, match="result evidence"):
        repository.mark_succeeded(
            attempt.attempt_id,
            _result(approved, health, provider_version="different-v2"),
        )
    still_running = repository.get_attempt(attempt.attempt_id)
    assert still_running is not None and still_running.state == "running"

    with pytest.raises(StorageConflictError, match="result evidence"):
        repository.mark_succeeded(
            attempt.attempt_id,
            _result(approved, health, idempotency_key=f"cfp-{'f' * 64}"),
        )


def test_pr27_state_machine_rejects_invalid_transitions_and_provider_identity(tmp_path) -> None:
    repository = _repository(tmp_path)
    approved = _approved()
    attempt = repository.prepare_attempt(approved)

    with pytest.raises(StorageConflictError, match="target provider"):
        repository.mark_running(
            attempt.attempt_id,
            PublishingProviderHealth(provider_id="other", provider_version="1", available=True),
        )
    with pytest.raises(StorageConflictError, match="unavailable"):
        repository.mark_running(
            attempt.attempt_id,
            PublishingProviderHealth(provider_id="fixture", provider_version="1", available=False),
        )

    repository.mark_running(attempt.attempt_id, _health())
    with pytest.raises(StorageConflictError, match="expected prepared"):
        repository.mark_running(attempt.attempt_id, _health())
    with pytest.raises(StorageConflictError, match="expected running"):
        repository.mark_succeeded(new_entity_id(EntityKind.PUBLISH), _result(approved, _health()))
