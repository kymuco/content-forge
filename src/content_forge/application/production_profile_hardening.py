"""Reversible PR25 production-profile binding semantics."""

from __future__ import annotations

from datetime import datetime, timezone

from content_forge.core import Project, ProjectState
from content_forge.storage import LocalLibrary
from content_forge.templates import RegistryBundle

from . import production_profile as _base

_PROFILE_MUTABLE_STATES = frozenset(
    {ProjectState.INBOX, ProjectState.DRAFT, ProjectState.PREPARED}
)


class ProductionProfileRegistry(_base.ProductionProfileRegistry):
    """Final registry surface with validated latest-revision listing."""

    def list_latest(self, *, limit: int = 256) -> tuple[_base.ProductionProfileRevision, ...]:
        if limit < 1 or limit > 256:
            raise _base.ProductionProfileValidationError(
                "production profile list limit is outside allowed range"
            )
        with self.library.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT current.manifest_json
                FROM production_profile_revisions AS current
                WHERE current.revision = (
                    SELECT MAX(candidate.revision)
                    FROM production_profile_revisions AS candidate
                    WHERE candidate.profile_id = current.profile_id
                )
                ORDER BY current.profile_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items = tuple(
            self.repository._decode(str(row["manifest_json"])) for row in rows
        )
        for item in items:
            self._validate_definition(item.definition)
        return items


class ProductionProfileWorkflow(_base.ProductionProfileWorkflow):
    """Final PR25 workflow with reversible ownership of filled defaults."""

    def __init__(self, library: LocalLibrary, registries: RegistryBundle) -> None:
        self.library = library
        self.registry = ProductionProfileRegistry(library, registries)

    def _validate_snapshot_revision(
        self,
        manifest: _base.ProjectProductionProfileManifest,
        *,
        validate_external: bool,
    ) -> None:
        # Read the immutable revision record directly first. Safe removal must remain
        # possible even when an old optional cast/music/watermark dependency disappeared;
        # those external references are required only when the profile is still being used.
        current = self.registry.repository.get(
            manifest.revision.profile_id,
            manifest.revision.revision,
        )
        if current is None or current != manifest.revision:
            raise _base.ProductionProfileConflictError(
                "project production profile snapshot no longer matches registry revision"
            )
        if validate_external:
            self.registry._validate_definition(current.definition)

    def _base_project(
        self,
        project: Project,
        manifest: _base.ProjectProductionProfileManifest | None,
    ) -> Project:
        if manifest is None:
            return project
        self._validate_materialized(project, manifest)
        self._validate_snapshot_revision(manifest, validate_external=False)
        metadata = project.model_dump(mode="json")["metadata"]
        if not isinstance(metadata, dict):  # pragma: no cover - Project contract
            raise _base.ProductionProfileValidationError("project metadata is malformed")
        metadata.pop(_base.PROFILE_METADATA_KEY, None)
        return project.validated_copy(
            update={
                "template": None if manifest.applied_default_template else project.template,
                "output_profiles": (
                    () if manifest.applied_output_profiles else project.output_profiles
                ),
                "metadata": metadata,
            }
        )

    @staticmethod
    def _same_revision(
        manifest: _base.ProjectProductionProfileManifest,
        revision: _base.ProductionProfileRevision,
    ) -> bool:
        return (
            manifest.revision.profile_id == revision.profile_id
            and manifest.revision.revision == revision.revision
            and manifest.revision.definition_sha256 == revision.definition_sha256
        )

    def manifest(self, project_id: str) -> _base.ProjectProductionProfileManifest | None:
        project, _ = self._snapshot(project_id)
        manifest = _base.production_profile_manifest(project)
        if manifest is not None:
            self._validate_materialized(project, manifest)
            self._validate_snapshot_revision(manifest, validate_external=True)
        return manifest

    def bind(
        self,
        project_id: str,
        profile_id: str,
        *,
        revision: int | None = None,
    ) -> _base.ProjectProductionProfileManifest:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _PROFILE_MUTABLE_STATES:
            raise _base.ProductionProfileConflictError(
                f"production profile cannot mutate project in state {project.state.value}"
            )
        existing = _base.production_profile_manifest(project)
        target = self.registry.get(profile_id, revision)
        if existing is not None and self._same_revision(existing, target):
            self._validate_materialized(project, existing)
            self._validate_snapshot_revision(existing, validate_external=True)
            return existing

        base = self._base_project(project, existing)
        definition = target.definition
        apply_template = base.template is None and definition.default_template is not None
        apply_outputs = not base.output_profiles and bool(definition.output_profiles)
        manifest = _base.ProjectProductionProfileManifest(
            project_id=base.project_id,
            revision=target,
            applied_default_template=apply_template,
            applied_output_profiles=apply_outputs,
        )
        metadata = base.model_dump(mode="json")["metadata"]
        if not isinstance(metadata, dict):  # pragma: no cover - Project contract
            raise _base.ProductionProfileValidationError("project metadata is malformed")
        metadata[_base.PROFILE_METADATA_KEY] = manifest.model_dump(mode="json")
        updated = base.validated_copy(
            update={
                "template": definition.default_template if apply_template else base.template,
                "output_profiles": (
                    definition.output_profiles if apply_outputs else base.output_profiles
                ),
                "metadata": metadata,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._cas_project(expected_json, updated)
        return manifest

    def unbind(self, project_id: str) -> Project:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _PROFILE_MUTABLE_STATES:
            raise _base.ProductionProfileConflictError(
                f"production profile cannot mutate project in state {project.state.value}"
            )
        manifest = _base.production_profile_manifest(project)
        if manifest is None:
            raise _base.ProductionProfileNotFoundError(
                f"project has no production profile snapshot: {project_id}"
            )
        base = self._base_project(project, manifest)
        updated = base.validated_copy(update={"updated_at": datetime.now(timezone.utc)})
        return self._cas_project(expected_json, updated)


__all__ = ["ProductionProfileRegistry", "ProductionProfileWorkflow"]
