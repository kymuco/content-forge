"""Persistent synchronous orchestration for preview/final render attempts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
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
    LocalLibrary,
    StorageConflictError,
    StoredJob,
    sha256_file,
    transition_job_state,
)
from content_forge.timeline import RenderPlan, render_plan_digest

from .models import (
    RenderArtifactManifest,
    RenderFailureManifest,
    RenderPurpose,
    RenderSourceFingerprint,
)


class RenderOrchestrationError(RuntimeError):
    """Base class for render-job persistence/orchestration failures."""


class RenderJobStateError(RenderOrchestrationError):
    pass


class RenderJobIntegrityError(RenderOrchestrationError):
    pass


@dataclass(frozen=True, slots=True)
class _RenderJobPaths:
    directory_key: str
    plan_key: str
    output_key: str
    manifest_key: str
    failure_key: str

    @classmethod
    def for_job(cls, project_id: str, job_id: str, container: str) -> "_RenderJobPaths":
        directory = f"renders/{project_id}/{job_id}"
        return cls(
            directory_key=directory,
            plan_key=f"{directory}/plan.json",
            output_key=f"{directory}/artifact.{container}",
            manifest_key=f"{directory}/artifact-manifest.json",
            failure_key=f"{directory}/failure-manifest.json",
        )


def _strict_json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_strict_json_text(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_descriptor = None
            if directory_descriptor is not None:
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_model(path: Path, model: object) -> None:
    payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    _atomic_write_json(path, payload)


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RenderJobIntegrityError(f"render job payload has invalid {key}")
    return value


def _payload_optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RenderJobIntegrityError(f"render job payload has invalid {key}")
    return value


class RenderOrchestrator:
    """Persist a RenderPlan, execute it once, and publish an audited artifact."""

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library

    def submit(self, plan: RenderPlan, *, purpose: RenderPurpose) -> StoredJob:
        """Persist one immutable render-plan snapshot as a queued job."""

        project = self.library.load_project(plan.project_id)
        if project is None:
            raise RenderJobIntegrityError(
                f"render plan project is not stored in the local library: {plan.project_id}"
            )

        profile_purpose = plan.output_profile.properties.get("purpose")
        if profile_purpose != purpose:
            raise RenderJobIntegrityError(
                "render purpose must match output profile properties['purpose']"
            )
        stored_profile = next(
            (
                profile
                for profile in project.output_profiles
                if profile.profile_id == plan.output_profile.profile_id
            ),
            None,
        )
        if stored_profile is None:
            raise RenderJobIntegrityError(
                "render plan output profile is not present in the stored project"
            )
        if stored_profile != plan.output_profile:
            raise RenderJobIntegrityError(
                "render plan output profile differs from the stored project profile"
            )
        if plan.variant_id is not None and plan.variant_id not in {
            variant.variant_id for variant in project.variants
        }:
            raise RenderJobIntegrityError(
                "render plan variant is not present in the stored project"
            )
        project_template_id = None if project.template is None else project.template.template_id
        project_template_version = None if project.template is None else project.template.version
        if (plan.template_id, plan.template_version) != (
            project_template_id,
            project_template_version,
        ):
            raise RenderJobIntegrityError(
                "render plan template identity does not match the stored project"
            )

        for planned_asset in plan.assets:
            stored_asset = self.library.database.get_asset(planned_asset.asset_id)
            if stored_asset is None:
                raise RenderJobIntegrityError(
                    f"render plan asset is not stored in the local library: {planned_asset.asset_id}"
                )
            if stored_asset.sha256 != planned_asset.sha256:
                raise RenderJobIntegrityError(
                    f"render plan asset digest differs from library metadata: {planned_asset.asset_id}"
                )
            if stored_asset.storage_key != planned_asset.storage_key:
                raise RenderJobIntegrityError(
                    f"render plan asset storage key differs from library metadata: {planned_asset.asset_id}"
                )

        digest = render_plan_digest(plan)
        job = StoredJob(
            project_id=plan.project_id,
            job_type="render",
            state="queued",
            payload={},
        )
        paths = _RenderJobPaths.for_job(
            plan.project_id,
            job.job_id,
            plan.output_profile.container,
        )
        payload = {
            "purpose": purpose,
            "profile_id": plan.output_profile.profile_id,
            "variant_id": plan.variant_id,
            "template_id": plan.template_id,
            "template_version": plan.template_version,
            "render_plan_digest": digest,
            "plan_storage_key": paths.plan_key,
            "output_storage_key": paths.output_key,
            "manifest_storage_key": paths.manifest_key,
            "failure_storage_key": paths.failure_key,
        }
        job = job.validated_copy(update={"payload": payload})

        directory = self.library.paths.root / paths.directory_key
        created_directory = False
        try:
            directory.mkdir(parents=True, exist_ok=False)
            created_directory = True
            _atomic_write_json(directory / "plan.json", plan.model_dump(mode="json"))
            self.library.database.create_job(job)
        except BaseException:
            if created_directory:
                shutil.rmtree(directory, ignore_errors=True)
            raise
        return job

    def _job(self, job_id: str) -> StoredJob:
        job = self.library.database.get_job(job_id)
        if job is None:
            raise RenderJobIntegrityError(f"unknown render job: {job_id}")
        if job.job_type != "render":
            raise RenderJobIntegrityError(f"job is not a render job: {job_id}")
        if job.project_id is None:
            raise RenderJobIntegrityError("render job has no project ID")
        return job

    def _paths_from_job(self, job: StoredJob) -> _RenderJobPaths:
        payload = job.payload
        purpose = _payload_string(payload, "purpose")
        if purpose not in {"preview", "final"}:
            raise RenderJobIntegrityError("render job purpose is invalid")
        profile_id = _payload_string(payload, "profile_id")
        plan_key = _payload_string(payload, "plan_storage_key")
        output_key = _payload_string(payload, "output_storage_key")
        manifest_key = _payload_string(payload, "manifest_storage_key")
        failure_key = _payload_string(payload, "failure_storage_key")

        output_suffix = Path(output_key).suffix
        if not output_suffix or output_suffix == ".":
            raise RenderJobIntegrityError("render job output storage key has no container suffix")
        container = output_suffix[1:]
        expected = _RenderJobPaths.for_job(job.project_id or "", job.job_id, container)
        if (
            plan_key != expected.plan_key
            or output_key != expected.output_key
            or manifest_key != expected.manifest_key
            or failure_key != expected.failure_key
        ):
            raise RenderJobIntegrityError("render job storage keys are not canonical")
        if not profile_id:
            raise RenderJobIntegrityError("render job profile ID is empty")
        return expected

    def _load_plan(self, job: StoredJob, paths: _RenderJobPaths) -> RenderPlan:
        plan_path = self.library.paths.root / paths.plan_key
        try:
            plan = RenderPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RenderJobIntegrityError(f"failed to load persisted render plan: {exc}") from exc

        payload = job.payload
        if plan.project_id != job.project_id:
            raise RenderJobIntegrityError("persisted render plan project ID changed")
        if plan.output_profile.profile_id != _payload_string(payload, "profile_id"):
            raise RenderJobIntegrityError("persisted render plan profile ID changed")
        if plan.variant_id != _payload_optional_string(payload, "variant_id"):
            raise RenderJobIntegrityError("persisted render plan variant ID changed")
        if plan.template_id != _payload_optional_string(payload, "template_id"):
            raise RenderJobIntegrityError("persisted render plan template ID changed")
        if plan.template_version != _payload_optional_string(payload, "template_version"):
            raise RenderJobIntegrityError("persisted render plan template version changed")
        digest = render_plan_digest(plan)
        if digest != _payload_string(payload, "render_plan_digest"):
            raise RenderJobIntegrityError("persisted render plan digest changed")
        if plan.output_profile.container != Path(paths.output_key).suffix[1:]:
            raise RenderJobIntegrityError("persisted render plan container changed")
        if plan.output_profile.properties.get("purpose") != _payload_string(payload, "purpose"):
            raise RenderJobIntegrityError("persisted render plan purpose changed")
        return plan

    def _write_failure(
        self,
        job: StoredJob,
        paths: _RenderJobPaths,
        exc: BaseException,
    ) -> RenderFailureManifest:
        payload = job.payload
        code = "orchestration_failed"
        stage = "orchestration"
        return_code = None
        details: Mapping[str, object] = {}
        state = "failed"
        if isinstance(exc, FFmpegBackendError):
            code = exc.error.code
            stage = exc.error.stage
            return_code = exc.error.return_code
            details = exc.error.details
            if code == "render_cancelled":
                state = "cancelled"

        message = str(exc).strip() or type(exc).__name__
        failure = RenderFailureManifest(
            job_id=job.job_id,
            project_id=job.project_id or "",
            purpose=_payload_string(payload, "purpose"),
            profile_id=_payload_string(payload, "profile_id"),
            render_plan_digest=_payload_string(payload, "render_plan_digest"),
            failure_storage_key=paths.failure_key,
            state=state,
            code=code,
            stage=stage,
            message=message[:8192],
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            return_code=return_code,
            details=details,
        )
        _atomic_write_model(self.library.paths.root / paths.failure_key, failure)
        return failure

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
        """Atomically claim and execute one queued render job."""

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

        output_path = self.library.paths.root / paths.output_key
        manifest_path = self.library.paths.root / paths.manifest_key
        failure_path = self.library.paths.root / paths.failure_key
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
                command_manifest_digest=command_manifest_digest(command),
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
            failure_path.unlink(missing_ok=True)
            transition_job_state(
                self.library.database,
                job.job_id,
                expected_state="running",
                state="succeeded",
            )
            return artifact
        except BaseException as exc:
            output_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            terminal_state = "failed"
            try:
                failure = self._write_failure(job, paths, exc)
                terminal_state = failure.state
            except BaseException:
                pass
            try:
                transition_job_state(
                    self.library.database,
                    job.job_id,
                    expected_state="running",
                    state=terminal_state,
                )
            except BaseException:
                pass
            raise

    def load_plan(self, job_id: str) -> RenderPlan:
        job = self._job(job_id)
        paths = self._paths_from_job(job)
        return self._load_plan(job, paths)

    def load_artifact(self, job_id: str) -> RenderArtifactManifest | None:
        job = self._job(job_id)
        if job.state != "succeeded":
            return None
        paths = self._paths_from_job(job)
        plan = self._load_plan(job, paths)
        path = self.library.paths.root / paths.manifest_key
        try:
            artifact = RenderArtifactManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RenderJobIntegrityError(f"failed to load artifact manifest: {exc}") from exc
        if artifact.job_id != job.job_id or artifact.project_id != job.project_id:
            raise RenderJobIntegrityError("artifact manifest identity does not match job")
        if artifact.manifest_storage_key != paths.manifest_key:
            raise RenderJobIntegrityError("artifact manifest storage key does not match job")
        if artifact.output_storage_key != paths.output_key:
            raise RenderJobIntegrityError("artifact output storage key does not match job")
        if artifact.purpose != _payload_string(job.payload, "purpose"):
            raise RenderJobIntegrityError("artifact purpose does not match job")
        if artifact.profile_id != plan.output_profile.profile_id:
            raise RenderJobIntegrityError("artifact profile does not match persisted plan")
        if artifact.render_plan_digest != render_plan_digest(plan):
            raise RenderJobIntegrityError("artifact render-plan digest does not match persisted plan")
        if (artifact.variant_id, artifact.variant_language) != (
            plan.variant_id,
            plan.variant_language,
        ):
            raise RenderJobIntegrityError("artifact variant identity does not match persisted plan")
        if (artifact.template_id, artifact.template_version) != (
            plan.template_id,
            plan.template_version,
        ):
            raise RenderJobIntegrityError("artifact template identity does not match persisted plan")
        expected_sources = tuple(
            RenderSourceFingerprint(
                asset_id=asset.asset_id,
                sha256=asset.sha256,
                storage_key=asset.storage_key,
            )
            for asset in sorted(plan.assets, key=lambda item: item.asset_id)
        )
        if artifact.source_assets != expected_sources:
            raise RenderJobIntegrityError("artifact source fingerprints do not match persisted plan")
        output_path = self.library.paths.root / paths.output_key
        if not output_path.is_file():
            raise RenderJobIntegrityError("successful render artifact is missing")
        if output_path.stat().st_size != artifact.bytes_written:
            raise RenderJobIntegrityError("successful render artifact size changed")
        if sha256_file(output_path) != artifact.output_sha256:
            raise RenderJobIntegrityError("successful render artifact digest changed")
        return artifact

    def load_failure(self, job_id: str) -> RenderFailureManifest | None:
        job = self._job(job_id)
        if job.state not in {"failed", "cancelled"}:
            return None
        paths = self._paths_from_job(job)
        path = self.library.paths.root / paths.failure_key
        if not path.is_file():
            return None
        try:
            failure = RenderFailureManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RenderJobIntegrityError(f"failed to load failure manifest: {exc}") from exc
        if failure.job_id != job.job_id or failure.project_id != job.project_id:
            raise RenderJobIntegrityError("failure manifest identity does not match job")
        if failure.failure_storage_key != paths.failure_key:
            raise RenderJobIntegrityError("failure manifest storage key does not match job")
        if failure.state != job.state:
            raise RenderJobIntegrityError("failure manifest state does not match job")
        if failure.purpose != _payload_string(job.payload, "purpose"):
            raise RenderJobIntegrityError("failure manifest purpose does not match job")
        if failure.profile_id != _payload_string(job.payload, "profile_id"):
            raise RenderJobIntegrityError("failure manifest profile does not match job")
        if failure.render_plan_digest != _payload_string(job.payload, "render_plan_digest"):
            raise RenderJobIntegrityError("failure manifest render-plan digest does not match job")
        return failure
