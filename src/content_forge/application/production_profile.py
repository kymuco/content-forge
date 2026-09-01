"""PR25 reusable project/series/channel production-profile contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from content_forge.application.voice_cast_registry import VoiceCastRegistry
from content_forge.core import (
    Asset,
    EntityKind,
    LanguageTag,
    MediaType,
    OutputProfile,
    Project,
    ProjectState,
    RegistryKey,
    TemplateRef,
    dump_json,
    load_json,
    require_entity_id,
)
from content_forge.core.models import FrozenModel, SHA256
from content_forge.storage import LocalLibrary, StorageSchemaError
from content_forge.templates import RegistryBundle, SkinRef

PROFILE_DEFINITION_VERSION = "pr25_production_profile_definition_v1"
PROFILE_REVISION_VERSION = "pr25_production_profile_revision_v1"
PROFILE_MANIFEST_VERSION = "pr25_project_profile_manifest_v1"
PROFILE_METADATA_KEY = "pr25_production_profile"
MAX_PROFILE_ASSETS = 256
MAX_PROFILE_CAST_DEFAULTS = 128
MAX_PROFILE_LANGUAGES = 32
MAX_PROFILE_OUTPUTS = 16

ProfileScope = Literal["project", "series", "channel"]
CreditMode = Literal["source_required", "always"]


class ProductionProfileError(RuntimeError):
    pass


class ProductionProfileConflictError(ProductionProfileError):
    pass


class ProductionProfileNotFoundError(ProductionProfileError):
    pass


class ProductionProfileValidationError(ProductionProfileError):
    pass


class ProfileAssetPin(FrozenModel):
    asset_id: str
    sha256: SHA256
    role: RegistryKey

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.ASSET)


class ProfileCastDefault(FrozenModel):
    role: RegistryKey
    cast_id: RegistryKey
    cast_revision: int = Field(ge=1)
    cast_definition_sha256: SHA256


class ProfileCreditPolicy(FrozenModel):
    mode: CreditMode = "source_required"
    component_id: RegistryKey = "artist_credit"
    fallback_text: str | None = Field(default=None, max_length=2048)


class ProfileBranding(FrozenModel):
    display_name: str | None = Field(default=None, max_length=512)
    watermark: ProfileAssetPin | None = None
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)

    @field_validator("display_name")
    @classmethod
    def non_blank_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("branding display_name must contain non-whitespace content")
        return value


class ProductionProfileDefinition(FrozenModel):
    """Reusable defaults. This object is never itself live Project authority."""

    contract_version: Literal["pr25_production_profile_definition_v1"] = (
        PROFILE_DEFINITION_VERSION
    )
    profile_id: RegistryKey
    scope: ProfileScope
    display_name: str = Field(min_length=1, max_length=512)
    default_template: TemplateRef | None = None
    default_skin: SkinRef | None = None
    cast_defaults: tuple[ProfileCastDefault, ...] = Field(
        default=(), max_length=MAX_PROFILE_CAST_DEFAULTS
    )
    default_languages: tuple[LanguageTag, ...] = Field(
        default=(), max_length=MAX_PROFILE_LANGUAGES
    )
    credit_policy: ProfileCreditPolicy = Field(default_factory=ProfileCreditPolicy)
    output_profiles: tuple[OutputProfile, ...] = Field(
        default=(), max_length=MAX_PROFILE_OUTPUTS
    )
    branding: ProfileBranding = Field(default_factory=ProfileBranding)
    music_library: tuple[ProfileAssetPin, ...] = Field(
        default=(), max_length=MAX_PROFILE_ASSETS
    )
    reaction_library: tuple[ProfileAssetPin, ...] = Field(
        default=(), max_length=MAX_PROFILE_ASSETS
    )

    @field_validator("display_name")
    @classmethod
    def non_blank_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("production profile display_name must contain content")
        return value

    @model_validator(mode="after")
    def validate_definition(self):
        if self.default_skin is not None and self.default_template is None:
            raise ValueError("default_skin requires default_template")
        for values, label in (
            ([item.role for item in self.cast_defaults], "cast role"),
            (list(self.default_languages), "default language"),
            ([item.profile_id for item in self.output_profiles], "output profile ID"),
            ([item.asset_id for item in self.music_library], "music asset"),
            ([item.asset_id for item in self.reaction_library], "reaction asset"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate production profile {label}")
        return self


def production_profile_definition_digest(definition: ProductionProfileDefinition) -> str:
    encoded = json.dumps(
        definition.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProductionProfileRevision(FrozenModel):
    contract_version: Literal["pr25_production_profile_revision_v1"] = (
        PROFILE_REVISION_VERSION
    )
    profile_id: RegistryKey
    revision: int = Field(ge=1)
    definition_sha256: SHA256
    definition: ProductionProfileDefinition
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("production profile created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_revision(self):
        if self.profile_id != self.definition.profile_id:
            raise ValueError("production profile revision identity mismatch")
        if self.definition_sha256 != production_profile_definition_digest(self.definition):
            raise ValueError("production profile definition digest mismatch")
        return self


class ProjectProductionProfileManifest(FrozenModel):
    """Exact profile snapshot retained by one Project after explicit binding."""

    contract_version: Literal["pr25_project_profile_manifest_v1"] = PROFILE_MANIFEST_VERSION
    project_id: str
    revision: ProductionProfileRevision
    applied_default_template: bool = False
    applied_output_profiles: bool = False

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)


def production_profile_manifest(project: Project) -> ProjectProductionProfileManifest | None:
    raw = project.model_dump(mode="json")["metadata"].get(PROFILE_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ProductionProfileValidationError("stored PR25 production profile metadata is malformed")
    try:
        manifest = ProjectProductionProfileManifest.model_validate(raw)
    except Exception as exc:
        raise ProductionProfileValidationError(
            "stored PR25 production profile manifest is malformed"
        ) from exc
    if manifest.project_id != project.project_id:
        raise ProductionProfileConflictError("production profile manifest project mismatch")
    return manifest


class _ProductionProfileRepository:
    _SCHEMA_COMPONENT = "production_profile"
    _SCHEMA_VERSION = 1

    def __init__(self, library: LocalLibrary) -> None:
        self.library = library
        self.database = library.database

    def initialize(self) -> "_ProductionProfileRepository":
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM application_schema WHERE component = ?",
                (self._SCHEMA_COMPONENT,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > self._SCHEMA_VERSION:
                raise StorageSchemaError(
                    f"production profile schema {version} is newer than supported {self._SCHEMA_VERSION}"
                )
            if version not in {0, self._SCHEMA_VERSION}:
                raise StorageSchemaError(
                    f"unsupported production profile schema migration: {version} -> {self._SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS production_profile_revisions (
                    profile_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (profile_id, revision)
                )
                """
            )
            if version == 0:
                connection.execute(
                    "INSERT INTO application_schema(component, version) VALUES (?, ?)",
                    (self._SCHEMA_COMPONENT, self._SCHEMA_VERSION),
                )
        return self

    @staticmethod
    def _decode(raw: str) -> ProductionProfileRevision:
        try:
            return load_json(ProductionProfileRevision, raw)
        except Exception as exc:
            raise ProductionProfileValidationError(
                "stored production profile revision is malformed"
            ) from exc

    def get(self, profile_id: str, revision: int | None = None) -> ProductionProfileRevision | None:
        with self.database.connection() as connection:
            if revision is None:
                row = connection.execute(
                    """
                    SELECT manifest_json FROM production_profile_revisions
                    WHERE profile_id = ? ORDER BY revision DESC LIMIT 1
                    """,
                    (profile_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT manifest_json FROM production_profile_revisions
                    WHERE profile_id = ? AND revision = ?
                    """,
                    (profile_id, revision),
                ).fetchone()
        return None if row is None else self._decode(str(row["manifest_json"]))

    def put(self, definition: ProductionProfileDefinition) -> ProductionProfileRevision:
        digest = production_profile_definition_digest(definition)
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT manifest_json FROM production_profile_revisions
                WHERE profile_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (definition.profile_id,),
            ).fetchone()
            current = None if row is None else self._decode(str(row["manifest_json"]))
            if current is not None and current.definition_sha256 == digest:
                return current
            revision = ProductionProfileRevision(
                profile_id=definition.profile_id,
                revision=1 if current is None else current.revision + 1,
                definition_sha256=digest,
                definition=definition,
                created_at=datetime.now(timezone.utc),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO production_profile_revisions(
                        profile_id, revision, manifest_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        revision.profile_id,
                        revision.revision,
                        dump_json(revision),
                        revision.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProductionProfileConflictError(
                    "production profile revision could not be committed"
                ) from exc
        return revision


class ProductionProfileRegistry:
    """Immutable runtime-wide profile registry with exact external reference checks."""

    def __init__(self, library: LocalLibrary, registries: RegistryBundle) -> None:
        self.library = library
        self.registries = registries
        self.cast = VoiceCastRegistry(library)
        self.repository = _ProductionProfileRepository(library).initialize()

    def _validate_asset_pin(
        self,
        pin: ProfileAssetPin,
        *,
        allowed_types: frozenset[MediaType],
    ) -> Asset:
        asset = self.library.database.get_asset(pin.asset_id)
        if asset is None:
            raise ProductionProfileNotFoundError(f"unknown profile asset: {pin.asset_id}")
        if asset.sha256 != pin.sha256:
            raise ProductionProfileConflictError("profile asset digest no longer matches pin")
        if asset.media_type not in allowed_types:
            raise ProductionProfileValidationError(
                f"profile asset {pin.asset_id} has incompatible media type {asset.media_type.value}"
            )
        try:
            verified = self.library.assets.verify(asset)
        except (FileNotFoundError, OSError) as exc:
            raise ProductionProfileConflictError("profile asset bytes are unavailable") from exc
        if not verified:
            raise ProductionProfileConflictError("profile asset failed content verification")
        return asset

    def _validate_definition(self, definition: ProductionProfileDefinition) -> None:
        if definition.default_template is not None:
            try:
                registration = self.registries.templates.get(
                    definition.default_template.template_id,
                    definition.default_template.version,
                )
            except Exception as exc:
                raise ProductionProfileValidationError(
                    "production profile references an unknown template"
                ) from exc
            if definition.default_skin is not None:
                try:
                    self.registries.skins.get(
                        definition.default_skin.skin_id,
                        definition.default_skin.version,
                    )
                except Exception as exc:
                    raise ProductionProfileValidationError(
                        "production profile references an unknown skin"
                    ) from exc
                declared = {
                    (item.skin_id, item.version) for item in registration.definition.skins
                }
                if (
                    definition.default_skin.skin_id,
                    definition.default_skin.version,
                ) not in declared:
                    raise ProductionProfileValidationError(
                        "production profile default skin is not declared by default template"
                    )

        for item in definition.cast_defaults:
            try:
                revision = self.cast.get(item.cast_id, item.cast_revision)
            except Exception as exc:
                raise ProductionProfileValidationError(
                    "production profile references an unavailable cast revision"
                ) from exc
            if revision.definition_sha256 != item.cast_definition_sha256:
                raise ProductionProfileConflictError(
                    "production profile cast revision digest mismatch"
                )

        if definition.branding.watermark is not None:
            self._validate_asset_pin(
                definition.branding.watermark,
                allowed_types=frozenset({MediaType.IMAGE, MediaType.VIDEO}),
            )
        for pin in definition.music_library:
            self._validate_asset_pin(pin, allowed_types=frozenset({MediaType.AUDIO}))
        for pin in definition.reaction_library:
            self._validate_asset_pin(
                pin,
                allowed_types=frozenset({MediaType.IMAGE, MediaType.VIDEO}),
            )

    def put(self, definition: ProductionProfileDefinition) -> ProductionProfileRevision:
        self._validate_definition(definition)
        return self.repository.put(definition)

    def get(
        self,
        profile_id: str,
        revision: int | None = None,
    ) -> ProductionProfileRevision:
        item = self.repository.get(profile_id, revision)
        if item is None:
            suffix = "latest" if revision is None else str(revision)
            raise ProductionProfileNotFoundError(
                f"unknown production profile revision: {profile_id}@{suffix}"
            )
        self._validate_definition(item.definition)
        return item


_PROFILE_MUTABLE_STATES = frozenset(
    {ProjectState.INBOX, ProjectState.DRAFT, ProjectState.PREPARED}
)


class ProductionProfileWorkflow:
    """Bind one immutable profile revision as an explicit Project snapshot."""

    def __init__(self, library: LocalLibrary, registries: RegistryBundle) -> None:
        self.library = library
        self.registry = ProductionProfileRegistry(library, registries)

    def _snapshot(self, project_id: str) -> tuple[Project, str]:
        require_entity_id(project_id, EntityKind.PROJECT)
        with self.library.database.connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProductionProfileNotFoundError(f"unknown project: {project_id}")
        raw = str(row["manifest_json"])
        return load_json(Project, raw), raw

    def _cas_project(self, expected_json: str, updated: Project) -> Project:
        serialized = dump_json(updated)
        with self.library.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE projects
                SET content_kind = ?, state = ?, manifest_json = ?, updated_at = ?
                WHERE project_id = ? AND manifest_json = ?
                """,
                (
                    updated.content_kind,
                    updated.state.value,
                    serialized,
                    updated.updated_at.isoformat(),
                    updated.project_id,
                    expected_json,
                ),
            ).rowcount
            if changed != 1:
                raise ProductionProfileConflictError(
                    f"project changed concurrently: {updated.project_id}"
                )
        return updated

    @staticmethod
    def _validate_materialized(
        project: Project,
        manifest: ProjectProductionProfileManifest,
    ) -> None:
        definition = manifest.revision.definition
        if manifest.applied_default_template and project.template != definition.default_template:
            raise ProductionProfileConflictError(
                "project template drifted from PR25-applied profile default"
            )
        if manifest.applied_output_profiles and project.output_profiles != definition.output_profiles:
            raise ProductionProfileConflictError(
                "project output profiles drifted from PR25-applied profile defaults"
            )

    def manifest(self, project_id: str) -> ProjectProductionProfileManifest | None:
        project, _ = self._snapshot(project_id)
        manifest = production_profile_manifest(project)
        if manifest is not None:
            self._validate_materialized(project, manifest)
        return manifest

    def bind(
        self,
        project_id: str,
        profile_id: str,
        *,
        revision: int | None = None,
    ) -> ProjectProductionProfileManifest:
        project, expected_json = self._snapshot(project_id)
        if project.state not in _PROFILE_MUTABLE_STATES:
            raise ProductionProfileConflictError(
                f"production profile cannot mutate project in state {project.state.value}"
            )
        existing = production_profile_manifest(project)
        target = self.registry.get(profile_id, revision)
        if existing is not None:
            self._validate_materialized(project, existing)
            if (
                existing.revision.profile_id == target.profile_id
                and existing.revision.revision == target.revision
                and existing.revision.definition_sha256 == target.definition_sha256
            ):
                return existing
            raise ProductionProfileConflictError(
                "project already has a production profile snapshot; explicit rebind is deferred"
            )

        definition = target.definition
        apply_template = project.template is None and definition.default_template is not None
        apply_outputs = not project.output_profiles and bool(definition.output_profiles)
        manifest = ProjectProductionProfileManifest(
            project_id=project.project_id,
            revision=target,
            applied_default_template=apply_template,
            applied_output_profiles=apply_outputs,
        )
        metadata = project.model_dump(mode="json")["metadata"]
        if not isinstance(metadata, dict):  # pragma: no cover - Project contract
            raise ProductionProfileValidationError("project metadata is malformed")
        metadata[PROFILE_METADATA_KEY] = manifest.model_dump(mode="json")
        updated = project.validated_copy(
            update={
                "template": definition.default_template if apply_template else project.template,
                "output_profiles": (
                    definition.output_profiles if apply_outputs else project.output_profiles
                ),
                "metadata": metadata,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._cas_project(expected_json, updated)
        return manifest


__all__ = [
    "PROFILE_METADATA_KEY",
    "ProductionProfileConflictError",
    "ProductionProfileDefinition",
    "ProductionProfileError",
    "ProductionProfileNotFoundError",
    "ProductionProfileRegistry",
    "ProductionProfileRevision",
    "ProductionProfileValidationError",
    "ProductionProfileWorkflow",
    "ProfileAssetPin",
    "ProfileBranding",
    "ProfileCastDefault",
    "ProfileCreditPolicy",
    "ProjectProductionProfileManifest",
    "production_profile_definition_digest",
    "production_profile_manifest",
]
