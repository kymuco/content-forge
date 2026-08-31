"""Final public render orchestration process-control boundary."""

from __future__ import annotations

from content_forge.render.ffmpeg import (
    AssetPathSource,
    CancellationToken,
    FFmpegCapabilities,
)
from content_forge.storage import (
    StorageConflictError,
    StoredJob,
    sha256_file,
    transition_job_state,
)
from content_forge.timeline import render_plan_digest

from . import _render_jobs_hardened as hardened
from ._render_jobs_hardened import RenderOrchestrator as _HardenedRenderOrchestrator
from .models import RenderArtifactManifest


class RenderOrchestrator(_HardenedRenderOrchestrator):
    """Public orchestrator that never terminalizes process-control exceptions.

    The FFmpeg runner is still allowed to catch ``BaseException`` solely to terminate
    its child process, remove staging output, and immediately re-raise. This layer only
    converts ordinary application ``Exception`` failures into durable terminal render
    evidence. ``KeyboardInterrupt``, ``SystemExit``, and other process-control
    exceptions therefore leave a claimed render attempt in ``running`` so the PR17
    restart path can authenticate it later as ``render_interrupted``.
    """

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
            raise hardened.RenderJobStateError(
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
                hardened.RuntimeStorageResolver(self.library.paths)
                if asset_paths is None
                else asset_paths
            )
            command = hardened.compile_ffmpeg_command(
                plan,
                source,
                capabilities,
                output_path,
                prefer_nvenc=prefer_nvenc,
            )
            self._verify_command_source_bytes(command, plan)
            hardened._atomic_write_model(command_path, command)
            command_digest = hardened.command_manifest_digest(command)

            result = hardened.execute_ffmpeg(
                command,
                cancellation=cancellation,
                timeout=timeout,
            )
            probe = hardened.probe_media(
                output_path,
                ffprobe_path=capabilities.ffprobe_path,
            )
            if not probe.has_video:
                raise hardened.RenderJobIntegrityError(
                    "rendered artifact has no video stream"
                )
            if (probe.width, probe.height) != (
                plan.output_profile.width,
                plan.output_profile.height,
            ):
                raise hardened.RenderJobIntegrityError(
                    "rendered artifact dimensions do not match the output profile"
                )
            if probe.duration_seconds is None:
                raise hardened.RenderJobIntegrityError(
                    "rendered artifact has no probeable duration"
                )

            source_assets = tuple(
                hardened.RenderSourceFingerprint(
                    asset_id=asset.asset_id,
                    sha256=asset.sha256,
                    storage_key=asset.storage_key,
                )
                for asset in sorted(plan.assets, key=lambda item: item.asset_id)
            )
            artifact = RenderArtifactManifest(
                job_id=job.job_id,
                project_id=plan.project_id,
                purpose=hardened._payload_string(job.payload, "purpose"),
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
            hardened._atomic_write_model(manifest_path, artifact)
            artifact_digest = hardened._manifest_digest(artifact)

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
        except Exception as exc:
            # Application failures remain durable terminal attempts. Process-control
            # BaseExceptions deliberately bypass this block and remain recoverable.
            hardened._best_effort_unlink(output_path)
            hardened._best_effort_unlink(manifest_path)

            terminal_state = hardened._terminal_state(exc)
            failure_digest: str | None = None
            try:
                failure = self._write_failure(job, paths, exc)
                terminal_state = failure.state
                failure_digest = hardened._manifest_digest(failure)
            except Exception:
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
            except Exception:
                pass
            raise


__all__ = ["RenderOrchestrator"]
