"""Final integrity hardening for persistent render orchestration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from content_forge.render.ffmpeg import (
    AssetPathSource,
    CancellationToken,
    FFmpegBackendError,
    FFmpegCapabilities,
    RuntimeStorageResolver,
    command_manifest_digest,
    compile_ffmpeg_command,
    execute_ffmpeg,
    probe_media,
)
from content_forge.storage import (
    StorageConflictError,
    StoredJob,
    sha256_file,
    transition_job_state,
)
from content_forge.timeline import RenderPlan, render_plan_digest

from ._render_jobs_base import (
    RenderJobIntegrityError,
    RenderJobStateError,
    RenderOrchestrationError,
    RenderOrchestrator as _BaseRenderOrchestrator,
    _atomic_write_model,
    _payload_string,
)
from .models import (
    RenderArtifactManifest,
    RenderFailureManifest,
    RenderPurpose,
    RenderSourceFingerprint,
)


def _manifest_digest(model: object) -> str:
    payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _terminal_state(exc: BaseException) -> str:
    if isinstance(exc, FFmpegBackendError) and exc.error.code == "render_cancelled":
        return "cancelled"
    return "failed"


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _plan_source_pairs(plan: RenderPlan) -> tuple[tuple[str | None, str], ...]:
    pairs: list[tuple[str | None, str]] = []
    for scene in plan.scenes:
        if scene.media_source_id is not None:
            pairs.append((scene.media_asset_id, scene.media_source_id))
    for overlay in plan.overlays:
        if overlay.source_id is not None:
            pairs.append((overlay.asset_id, overlay.source_id))
    for track in plan.audio_tracks:
        if track.source_id is not None:
            pairs.append((track.asset_id, track.source_id))
    return tuple(pairs)


class RenderOrchestrator(_BaseRenderOrchestrator):
    """Render orchestrator with terminal receipt and provenance authentication."""

    def submit(self, plan: RenderPlan, *, purpose: RenderPurpose) -> StoredJob:
        project = self.library.load_project(plan.project_id)
        if project is None:
            raise RenderJobIntegrityError(
                f"render plan project is not stored in the local library: {plan.project_id}"
            )

        if project.variants:
            if plan.variant_id is None or plan.variant_language is None:
                raise RenderJobIntegrityError(
                    "render plan must preserve the selected project variant identity"
                )
            stored_variant = next(
                (
                    variant
                    for variant in project.variants
                    if variant.variant_id == plan.variant_id
                ),
                None,
            )
            if stored_variant is None or stored_variant.language != plan.variant_language:
                raise RenderJobIntegrityError(
                    "render plan variant ID/language differs from the stored project"
                )
        elif plan.variant_id is not None or plan.variant_language is not None:
            raise RenderJobIntegrityError(
                "render plan carries variant identity for a project without variants"
            )

        project_sources = {
            record.source_id: record.asset_id for record in project.source_records
        }
        for asset_id, source_id in _plan_source_pairs(plan):
            if asset_id is None:
                raise RenderJobIntegrityError(
                    "render plan source reference has no corresponding asset"
                )
            project_asset_id = project_sources.get(source_id)
            if project_asset_id is None:
                raise RenderJobIntegrityError(
                    "render plan provenance source is not declared by the stored project: "
                    f"{source_id}"
                )
            if project_asset_id != asset_id:
                raise RenderJobIntegrityError(
                    "render plan provenance source does not match project asset reference"
                )
            source = self.library.database.get_source(source_id)
            if source is None:
                raise RenderJobIntegrityError(
                    f"render plan references unknown source provenance: {source_id}"
                )
            if source.asset_id != asset_id:
                raise RenderJobIntegrityError(
                    "render plan provenance source does not match referenced asset"
                )

        return super().submit(plan, purpose=purpose)

    def run_job(
        self,
        job_id: str,
        capabilities: FFmpegCapabilities,
        *,
        asset_paths: AssetPathSource | None = None,
        prefer_nvenc: bool = True,
        cancellation: CancellationToken | None = None,
        timeout: float | None = None,
    ) -> RenderArtifactManifest:
        """Claim, execute, verify, and atomically anchor one render attempt."""

        job = self._job(job_id)
        paths = self._paths_from_job(job)
        try:
            transition_job_state(
                self.library.database,
                job.job_id,
                expected_state="queued",
                state="running",
            )
        except StorageConflictError as exc:
            current = self.library.database.get_job(job.job_id)
            current_state = None if current is None else current.state
            raise RenderJobStateError(
                f"render job must be queued before execution, got {current_state!r}"
            ) from exc

        command_path = self.library.paths.root / paths.command_key
        output_path = self.library.paths.root / paths.output_key
        manifest_path = self.library.paths.root / paths.manifest_key
        failure_path = self.library.paths.root / paths.failure_key
        command_digest: str | None = None

        try:
            plan = self._load_plan(job, paths)
            source = (
                RuntimeStorageResolver(self.library.paths)
                if asset_paths is None
                else asset_paths
            )
            command = compile_ffmpeg_command(
                plan,
                source,
                capabilities,
                output_path,
                prefer_nvenc=prefer_nvenc,
            )
            self._verify_command_source_bytes(command, plan)
            _atomic_write_model(command_path, command)
            # Receipt authority begins only after the sidecar is durably published.
            command_digest = command_manifest_digest(command)

            result = execute_ffmpeg(
                command,
                cancellation=cancellation,
                timeout=timeout,
            )
            probe = probe_media(output_path, ffprobe_path=capabilities.ffprobe_path)
            if not probe.has_video:
                raise RenderJobIntegrityError("rendered artifact has no video stream")
            if (probe.width, probe.height) != (
                plan.output_profile.width,
                plan.output_profile.height,
            ):
                raise RenderJobIntegrityError(
                    "rendered artifact dimensions do not match the output profile"
                )
            if probe.duration_seconds is None:
                raise RenderJobIntegrityError("rendered artifact has no probeable duration")

            source_assets = tuple(
                RenderSourceFingerprint(
                    asset_id=asset.asset_id,
                    sha256=asset.sha256,
                    storage_key=asset.storage_key,
                )
                for asset in sorted(plan.assets, key=lambda item: item.asset_id)
            )
            artifact = RenderArtifactManifest(
                job_id=job.job_id,
                project_id=plan.project_id,
                purpose=_payload_string(job.payload, "purpose"),
                profile_id=plan.output_profile.profile_id,
                variant_id=plan.variant_id,
                variant_language=plan.variant_language,
                template_id=plan.template_id,
                template_version=plan.template_version,
                render_plan_digest=render_plan_digest(plan),
                command_manifest_digest=command_digest,
                command_manifest_storage_key=paths.command_key,
                output_sha256=sha256_file(output_path),
                output_storage_key=paths.output_key,
                manifest_storage_key=paths.manifest_key,
                video_encoder=command.video_encoder,
                ffmpeg_version=capabilities.ffmpeg_version,
                bytes_written=result.bytes_written,
                elapsed_seconds=result.elapsed_seconds,
                width=probe.width or 0,
                height=probe.height or 0,
                duration_seconds=probe.duration_seconds,
                fps=probe.fps,
                has_audio=probe.has_audio,
                video_codec=probe.video_codec,
                audio_codec=probe.audio_codec,
                source_assets=source_assets,
            )
            _atomic_write_model(manifest_path, artifact)
            artifact_digest = _manifest_digest(artifact)

            # A successful attempt may not coexist with stale failure evidence.
            failure_path.unlink(missing_ok=True)
            transition_job_state(
                self.library.database,
                job.job_id,
                expected_state="running",
                state="succeeded",
                payload_additions={
                    "command_manifest_digest": command_digest,
                    "artifact_manifest_digest": artifact_digest,
                },
            )
            return artifact
        except BaseException as exc:
            # Cleanup must never prevent publication of a terminal state.
            _best_effort_unlink(output_path)
            _best_effort_unlink(manifest_path)

            terminal_state = _terminal_state(exc)
            failure_digest: str | None = None
            try:
                failure = self._write_failure(job, paths, exc)
                terminal_state = failure.state
                failure_digest = _manifest_digest(failure)
            except BaseException:
                pass

            receipt: dict[str, object] = {}
            if command_digest is not None:
                receipt["command_manifest_digest"] = command_digest
            if failure_digest is not None:
                receipt["failure_manifest_digest"] = failure_digest
            try:
                transition_job_state(
                    self.library.database,
                    job.job_id,
                    expected_state="running",
                    state=terminal_state,
                    payload_additions=receipt,
                )
            except BaseException:
                pass
            raise

    def load_artifact(
        self,
        job_id: str,
        *,
        ffprobe_path: str = "ffprobe",
        probe_timeout: float = 20.0,
    ) -> RenderArtifactManifest | None:
        artifact = super().load_artifact(
            job_id,
            ffprobe_path=ffprobe_path,
            probe_timeout=probe_timeout,
        )
        if artifact is None:
            return None

        job = self._job(job_id)
        trusted_command_digest = _payload_string(
            job.payload,
            "command_manifest_digest",
        )
        trusted_artifact_digest = _payload_string(
            job.payload,
            "artifact_manifest_digest",
        )
        if artifact.command_manifest_digest != trusted_command_digest:
            raise RenderJobIntegrityError(
                "artifact command-manifest digest does not match authoritative job receipt"
            )
        if _manifest_digest(artifact) != trusted_artifact_digest:
            raise RenderJobIntegrityError(
                "artifact manifest digest does not match authoritative job receipt"
            )
        return artifact

    def load_failure(self, job_id: str) -> RenderFailureManifest | None:
        job = self._job(job_id)
        trusted_failure_digest = job.payload.get("failure_manifest_digest")
        failure = super().load_failure(job_id)
        if failure is None:
            if isinstance(trusted_failure_digest, str) and trusted_failure_digest:
                raise RenderJobIntegrityError(
                    "authenticated failure manifest is missing from persistent storage"
                )
            return None

        if not isinstance(trusted_failure_digest, str) or not trusted_failure_digest:
            raise RenderJobIntegrityError(
                "failure manifest is not authenticated by authoritative job state"
            )
        if _manifest_digest(failure) != trusted_failure_digest:
            raise RenderJobIntegrityError(
                "failure manifest digest does not match authoritative job receipt"
            )
        return failure


__all__ = [
    "RenderJobIntegrityError",
    "RenderJobStateError",
    "RenderOrchestrationError",
    "RenderOrchestrator",
]
