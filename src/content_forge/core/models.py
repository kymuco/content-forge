"""Validated, renderer-independent Content Forge domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from .enums import (
    AttentionMode,
    FitMode,
    MediaType,
    PermissionStatus,
    ProjectState,
    ReviewPriority,
    ReviewStatus,
)
from .ids import EntityKind, RegistryKey, new_entity_id, require_entity_id
from .versioning import CURRENT_SCHEMA_VERSION, SchemaVersion

SHA256 = Annotated[
    str,
    StringConstraints(
        to_lower=True,
        strip_whitespace=True,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    ),
]
LanguageTag = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=35,
        pattern=r"^(?:und|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)$",
    ),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _assert_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


class _FrozenDict(dict):
    """JSON-compatible dict that blocks ordinary in-place mutation."""

    __slots__ = ("_sealed",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError("canonical JSON containers are immutable")
        dict.__init__(self, *args, **kwargs)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_sealed" and not getattr(self, "_sealed", False):
            object.__setattr__(self, name, value)
            return
        raise TypeError("canonical JSON containers are immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("canonical JSON containers are immutable")

    @staticmethod
    def _immutable(*args: object, **kwargs: object) -> None:
        raise TypeError("canonical JSON containers are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class _FrozenList(list):
    """JSON-compatible list that blocks ordinary in-place mutation."""

    __slots__ = ("_sealed",)

    def __init__(self, iterable: object = ()) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError("canonical JSON containers are immutable")
        list.__init__(self, iterable)  # type: ignore[arg-type]
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_sealed" and not getattr(self, "_sealed", False):
            object.__setattr__(self, name, value)
            return
        raise TypeError("canonical JSON containers are immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("canonical JSON containers are immutable")

    @staticmethod
    def _immutable(*args: object, **kwargs: object) -> None:
        raise TypeError("canonical JSON containers are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_json_containers(value: object) -> object:
    """Recursively freeze JSON container values while preserving serialization shape."""

    if isinstance(value, (_FrozenDict, _FrozenList)):
        return value
    if isinstance(value, dict):
        return _FrozenDict(
            {key: _freeze_json_containers(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return _FrozenList(_freeze_json_containers(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json_containers(item) for item in value)
    return value


class FrozenModel(BaseModel):
    """Base class for canonical value objects.

    Models reject unknown fields and direct field assignment. JSON-compatible nested
    containers are recursively frozen after validation, so normal in-place mutation
    cannot bypass invariants. Use `validated_copy()` for copy-on-write updates.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        str_strip_whitespace=False,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def freeze_nested_containers(self) -> Self:
        for name in type(self).model_fields:
            current = getattr(self, name)
            frozen = _freeze_json_containers(current)
            if frozen is not current:
                object.__setattr__(self, name, frozen)
        return self

    def validated_copy(self, *, update: dict[str, object] | None = None) -> Self:
        """Return a fully revalidated copy with optional field updates.

        Pydantic's raw `model_copy(update=...)` intentionally trusts update data and does
        not run validation. Canonical Content Forge state must instead use this helper
        when applying dynamic or user-supplied changes.
        """

        payload = self.model_dump(mode="python", round_trip=True)
        if update:
            payload.update(update)
        return type(self).model_validate(payload)


class PersistedModel(FrozenModel):
    schema_version: SchemaVersion = CURRENT_SCHEMA_VERSION


class NormalizedPoint(FrozenModel):
    """Profile-independent point in normalized canvas coordinates [0, 1]."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class NormalizedRect(FrozenModel):
    """Profile-independent rectangle in normalized canvas coordinates [0, 1]."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def within_canvas(self) -> Self:
        epsilon = 1e-9
        if self.x + self.width > 1.0 + epsilon:
            raise ValueError("rectangle extends beyond normalized canvas width")
        if self.y + self.height > 1.0 + epsilon:
            raise ValueError("rectangle extends beyond normalized canvas height")
        return self


FULL_CANVAS = NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0)


class Asset(PersistedModel):
    """Immutable metadata identity for an ingested media object.

    PR3 will own filesystem ingest/storage. This model establishes the stable contract
    that the asset store will persist.
    """

    asset_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.ASSET))
    sha256: SHA256
    media_type: MediaType
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_seconds: float | None = Field(default=None, gt=0.0)
    fps: float | None = Field(default=None, gt=0.0)
    has_audio: bool | None = None
    storage_key: str | None = Field(default=None, min_length=1, max_length=1024)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.ASSET)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value)


class AssetRef(FrozenModel):
    """Reference to a library asset plus optional provenance and semantic role."""

    asset_id: str
    source_id: str | None = None
    role: RegistryKey = "primary"

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.ASSET)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str | None) -> str | None:
        if value is not None:
            return require_entity_id(value, EntityKind.SOURCE)
        return value


class SourceRecord(PersistedModel):
    """Provenance for an asset, independent of immutable asset bytes."""

    source_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.SOURCE))
    asset_id: str
    source_url: str | None = Field(default=None, max_length=4096)
    platform: str | None = Field(default=None, max_length=128)
    creator_name: str | None = Field(default=None, max_length=512)
    creator_handle: str | None = Field(default=None, max_length=512)
    original_title: str | None = Field(default=None, max_length=2048)
    collected_at: datetime = Field(default_factory=utc_now)
    credit_text: str | None = Field(default=None, max_length=2048)
    requires_credit: bool | None = None
    permission_status: PermissionStatus = PermissionStatus.UNKNOWN
    permission_note: str | None = Field(default=None, max_length=4096)
    notes: str | None = Field(default=None, max_length=8192)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.SOURCE)

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.ASSET)

    @field_validator("collected_at")
    @classmethod
    def validate_collected_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value)


class Variant(PersistedModel):
    """Language/presentation variant sharing project media and timing."""

    variant_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.VARIANT))
    language: LanguageTag = "und"
    hook: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=4096)
    description: str | None = Field(default=None, max_length=20000)
    hashtags: tuple[str, ...] = ()
    text_overrides: dict[str, str] = Field(default_factory=dict)
    style_overrides: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("variant_id")
    @classmethod
    def validate_variant_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.VARIANT)


class TemplateRef(FrozenModel):
    template_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)


class MotionSpec(FrozenModel):
    motion_type: RegistryKey
    start_rect: NormalizedRect | None = None
    end_rect: NormalizedRect | None = None
    focus: NormalizedPoint | None = None
    properties: dict[str, JsonValue] = Field(default_factory=dict)


class TransitionSpec(FrozenModel):
    transition_type: RegistryKey = "cut"
    duration_seconds: float = Field(default=0.0, ge=0.0)
    properties: dict[str, JsonValue] = Field(default_factory=dict)


class Overlay(PersistedModel):
    """Timed compositing component independent of concrete renderer commands."""

    overlay_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.OVERLAY))
    component_type: RegistryKey
    start_seconds: float = Field(default=0.0, ge=0.0)
    duration_seconds: float | None = Field(default=None, gt=0.0)
    placement: NormalizedRect | None = None
    z_index: int = 0
    text: str | None = None
    variant_field: RegistryKey | None = None
    asset_ref: AssetRef | None = None
    properties: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("overlay_id")
    @classmethod
    def validate_overlay_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.OVERLAY)


class AudioTrack(PersistedModel):
    """Renderer-independent audio placement/policy."""

    audio_track_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.AUDIO))
    track_type: RegistryKey
    asset_ref: AssetRef | None = None
    start_seconds: float = Field(default=0.0, ge=0.0)
    duration_seconds: float | None = Field(default=None, gt=0.0)
    gain_db: float = Field(default=0.0, ge=-120.0, le=24.0)
    loop: bool = False
    properties: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("audio_track_id")
    @classmethod
    def validate_audio_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.AUDIO)


class Scene(PersistedModel):
    """Ordered timed unit expressed in profile-independent geometry."""

    scene_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.SCENE))
    order: int = Field(ge=0)
    duration_seconds: float = Field(gt=0.0)
    media: AssetRef | None = None
    trim_start_seconds: float = Field(default=0.0, ge=0.0)
    trim_duration_seconds: float | None = Field(default=None, gt=0.0)
    placement: NormalizedRect = FULL_CANVAS
    fit_mode: FitMode = FitMode.COVER
    crop: NormalizedRect | None = None
    focus: NormalizedPoint | None = None
    motion: MotionSpec | None = None
    transition_in: TransitionSpec | None = None
    transition_out: TransitionSpec | None = None
    overlays: tuple[Overlay, ...] = ()
    audio_tracks: tuple[AudioTrack, ...] = ()
    properties: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("scene_id")
    @classmethod
    def validate_scene_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.SCENE)

    @model_validator(mode="after")
    def validate_local_ids(self) -> Self:
        _assert_unique([item.overlay_id for item in self.overlays], "scene overlay ID")
        _assert_unique(
            [item.audio_track_id for item in self.audio_tracks],
            "scene audio track ID",
        )
        return self


class SafeZone(FrozenModel):
    name: RegistryKey
    rect: NormalizedRect


class OutputProfile(PersistedModel):
    """Output geometry/encoding intent.

    Pixel dimensions live here, while Project/Scene geometry stays normalized. A later
    compiler resolves normalized coordinates independently for preview and final
    profiles, preventing preview/final drift.
    """

    profile_id: RegistryKey
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    fps: float = Field(gt=0.0, le=240.0)
    container: RegistryKey = "mp4"
    video_codec: RegistryKey = "h264"
    audio_codec: RegistryKey | None = "aac"
    video_bitrate_kbps: int | None = Field(default=None, ge=1)
    audio_bitrate_kbps: int | None = Field(default=None, ge=1)
    safe_zones: tuple[SafeZone, ...] = ()
    properties: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_safe_zone_names(self) -> Self:
        _assert_unique([zone.name for zone in self.safe_zones], "safe-zone name")
        return self


class ReviewSuggestion(PersistedModel):
    suggestion_id: str = Field(
        default_factory=lambda: new_entity_id(EntityKind.SUGGESTION)
    )
    label: str = Field(min_length=1, max_length=4096)
    value: JsonValue
    provider: str | None = Field(default=None, max_length=512)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("suggestion_id")
    @classmethod
    def validate_suggestion_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.SUGGESTION)


class ReviewTask(PersistedModel):
    """A bounded human-attention request, independent of project lifecycle state."""

    review_task_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.REVIEW))
    project_id: str
    task_type: RegistryKey
    status: ReviewStatus = ReviewStatus.OPEN
    attention: AttentionMode = AttentionMode.REVIEW
    priority: ReviewPriority = ReviewPriority.NORMAL
    blocking: bool = True
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    suggestions: tuple[ReviewSuggestion, ...] = ()
    accepted_value: JsonValue | None = None
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None

    @field_validator("review_task_id")
    @classmethod
    def validate_review_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.REVIEW)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("created_at", "resolved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return _ensure_aware(value)
        return value

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if self.status is ReviewStatus.OPEN and self.resolved_at is not None:
            raise ValueError("open review task cannot have resolved_at")
        if self.status in {ReviewStatus.RESOLVED, ReviewStatus.CANCELLED}:
            if self.resolved_at is None:
                raise ValueError("closed review task requires resolved_at")
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("resolved_at cannot be before created_at")
        _assert_unique(
            [item.suggestion_id for item in self.suggestions],
            "review suggestion ID",
        )
        return self


class Project(PersistedModel):
    """Canonical production manifest.

    This is intentionally renderer-independent. SQLite normalization arrives in PR3;
    JSON/YAML manifests already round-trip losslessly in PR2.
    """

    project_id: str = Field(default_factory=lambda: new_entity_id(EntityKind.PROJECT))
    content_kind: RegistryKey
    state: ProjectState = ProjectState.INBOX
    source_refs: tuple[AssetRef, ...] = ()
    source_records: tuple[SourceRecord, ...] = ()
    variants: tuple[Variant, ...] = ()
    workflow_id: RegistryKey | None = None
    template: TemplateRef | None = None
    scenes: tuple[Scene, ...] = ()
    overlays: tuple[Overlay, ...] = ()
    audio_tracks: tuple[AudioTrack, ...] = ()
    output_profiles: tuple[OutputProfile, ...] = ()
    review_tasks: tuple[ReviewTask, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime) -> datetime:
        return _ensure_aware(value)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")

        _assert_unique([item.source_id for item in self.source_records], "source ID")
        _assert_unique([item.variant_id for item in self.variants], "variant ID")
        _assert_unique([item.scene_id for item in self.scenes], "scene ID")
        _assert_unique([str(item.order) for item in self.scenes], "scene order")
        _assert_unique(
            [item.profile_id for item in self.output_profiles],
            "output profile ID",
        )
        _assert_unique(
            [item.review_task_id for item in self.review_tasks],
            "review task ID",
        )

        all_overlays = list(self.overlays)
        all_audio_tracks = list(self.audio_tracks)
        for scene in self.scenes:
            all_overlays.extend(scene.overlays)
            all_audio_tracks.extend(scene.audio_tracks)

        _assert_unique(
            [item.overlay_id for item in all_overlays],
            "overlay ID across project",
        )
        _assert_unique(
            [item.audio_track_id for item in all_audio_tracks],
            "audio track ID across project",
        )
        _assert_unique(
            [
                suggestion.suggestion_id
                for task in self.review_tasks
                for suggestion in task.suggestions
            ],
            "review suggestion ID across project",
        )

        for task in self.review_tasks:
            if task.project_id != self.project_id:
                raise ValueError(
                    "review task project_id must match containing project_id"
                )

        source_asset_ids = {ref.asset_id for ref in self.source_refs}
        for record in self.source_records:
            if record.asset_id not in source_asset_ids:
                raise ValueError(
                    "source record asset_id must appear in project source_refs"
                )

        source_asset_by_id = {
            record.source_id: record.asset_id for record in self.source_records
        }
        referenced_assets: list[AssetRef] = list(self.source_refs)
        for scene in self.scenes:
            if scene.media is not None:
                referenced_assets.append(scene.media)
            referenced_assets.extend(
                overlay.asset_ref
                for overlay in scene.overlays
                if overlay.asset_ref is not None
            )
            referenced_assets.extend(
                track.asset_ref
                for track in scene.audio_tracks
                if track.asset_ref is not None
            )
        referenced_assets.extend(
            overlay.asset_ref for overlay in self.overlays if overlay.asset_ref is not None
        )
        referenced_assets.extend(
            track.asset_ref for track in self.audio_tracks if track.asset_ref is not None
        )

        for ref in referenced_assets:
            if ref.source_id is None:
                continue
            source_asset_id = source_asset_by_id.get(ref.source_id)
            if source_asset_id is None:
                raise ValueError(
                    "asset reference source_id must identify a project source record"
                )
            if source_asset_id != ref.asset_id:
                raise ValueError(
                    "asset reference source_id must belong to the referenced asset_id"
                )

        return self
