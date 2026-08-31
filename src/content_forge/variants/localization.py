"""PR16 language-variant metadata, font intent, and cache identity contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from content_forge.core import (
    EntityKind,
    Overlay,
    Project,
    RegistryKey,
    Scene,
    Variant,
    require_entity_id,
)
from content_forge.core.models import FrozenModel, LanguageTag

if TYPE_CHECKING:
    from content_forge.timeline import AssetSource, RenderPlan, ResolvedTemplate

LOCALIZED_VARIANT_CONTRACT_VERSION = "1"
VARIANT_RENDER_CACHE_KEY_VERSION = "1"
SUBTITLE_TEXT_OVERRIDE_KEY = "subtitle"
FONT_STYLE_OVERRIDE_KEY = "font"

LocalizedVariantContractVersion = Literal["1"]
VariantRenderCacheKeyVersion = Literal["1"]
LocalizedText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=30000),
]
LocalizedHashtag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
FontFamilyToken = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    ),
]

_MAX_TEXT_OVERRIDES = 128
_MAX_STYLE_OVERRIDES = 64
_MAX_HASHTAGS = 32
_FONT_ADAPTER = TypeAdapter(FontFamilyToken)


class VariantLocalizationError(ValueError):
    """Localized variant data cannot satisfy the PR16 language-variant contract."""


class VariantCacheIdentityError(VariantLocalizationError):
    """A compiled language variant cannot form the requested cache identity."""


def _font_token(value: object) -> str:
    if not isinstance(value, str):
        raise VariantLocalizationError("variant font style override must be a string")
    try:
        return _FONT_ADAPTER.validate_python(value, strict=True)
    except ValueError as exc:
        raise VariantLocalizationError(
            "variant font must be a portable family token, not a filesystem path"
        ) from exc


class LocalizedVariantSnapshot(FrozenModel):
    """Immutable localized metadata snapshot independent of shared media/timeline state.

    The snapshot deliberately contains no scenes, asset references, source records, or
    output profiles. Those remain on the containing Project and are shared by every
    language variant.
    """

    contract_version: LocalizedVariantContractVersion = LOCALIZED_VARIANT_CONTRACT_VERSION
    variant_id: str
    language: LanguageTag
    hook: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=4096)
    description: str | None = Field(default=None, max_length=20000)
    hashtags: tuple[LocalizedHashtag, ...] = Field(default=(), max_length=_MAX_HASHTAGS)
    text_overrides: Mapping[RegistryKey, LocalizedText] = Field(default_factory=dict)
    style_overrides: Mapping[RegistryKey, JsonValue] = Field(default_factory=dict)

    @field_validator("variant_id")
    @classmethod
    def validate_variant_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.VARIANT)

    @field_validator("style_overrides")
    @classmethod
    def normalize_reserved_style_overrides(
        cls,
        value: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        if FONT_STYLE_OVERRIDE_KEY not in value:
            return value
        normalized = dict(value)
        normalized[FONT_STYLE_OVERRIDE_KEY] = _font_token(
            normalized[FONT_STYLE_OVERRIDE_KEY]
        )
        return normalized

    @model_validator(mode="after")
    def validate_localized_contract(self) -> Self:
        if len(self.hashtags) != len(set(self.hashtags)):
            raise ValueError("localized hashtags must be unique after normalization")
        if len(self.text_overrides) > _MAX_TEXT_OVERRIDES:
            raise ValueError("localized text overrides exceed supported maximum")
        if len(self.style_overrides) > _MAX_STYLE_OVERRIDES:
            raise ValueError("localized style overrides exceed supported maximum")
        return self

    @property
    def subtitle(self) -> str | None:
        value = self.text_overrides.get(SUBTITLE_TEXT_OVERRIDE_KEY)
        return None if value is None else str(value)

    @property
    def font(self) -> str | None:
        value = self.style_overrides.get(FONT_STYLE_OVERRIDE_KEY)
        return None if value is None else _font_token(value)

    @classmethod
    def from_variant(cls, variant: Variant) -> "LocalizedVariantSnapshot":
        return cls(
            variant_id=variant.variant_id,
            language=variant.language,
            hook=variant.hook,
            title=variant.title,
            description=variant.description,
            hashtags=variant.hashtags,
            text_overrides=variant.text_overrides,
            style_overrides=variant.style_overrides,
        )


@dataclass(frozen=True, slots=True)
class CompiledLanguageVariant:
    """One atomic PR16 pair: resolved render plan plus its exact metadata snapshot."""

    plan: "RenderPlan"
    localized_variant: LocalizedVariantSnapshot

    def __post_init__(self) -> None:
        if self.plan.variant_id != self.localized_variant.variant_id:
            raise VariantLocalizationError(
                "compiled render plan variant_id does not match localized snapshot"
            )
        if self.plan.variant_language != self.localized_variant.language:
            raise VariantLocalizationError(
                "compiled render plan language does not match localized snapshot"
            )


class VariantRenderCacheIdentity(FrozenModel):
    """Variant-specific semantic cache identity consumed by later queue/cache layers."""

    cache_key_version: VariantRenderCacheKeyVersion = VARIANT_RENDER_CACHE_KEY_VERSION
    purpose: Literal["preview", "final"]
    project_id: str
    variant_id: str
    variant_language: LanguageTag
    profile_id: RegistryKey
    template_id: RegistryKey | None = None
    template_version: str | None = Field(default=None, min_length=1, max_length=64)
    render_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    localized_variant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.PROJECT)

    @field_validator("variant_id")
    @classmethod
    def validate_variant_id(cls, value: str) -> str:
        return require_entity_id(value, EntityKind.VARIANT)

    @model_validator(mode="after")
    def validate_template_pair(self) -> Self:
        if (self.template_id is None) != (self.template_version is None):
            raise ValueError("cache identity template ID/version must be present together")
        return self


def _canonical_digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[attr-defined]
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def localized_variant_snapshot(variant: Variant) -> LocalizedVariantSnapshot:
    """Materialize the bounded PR16 metadata contract from one canonical Variant."""

    try:
        return LocalizedVariantSnapshot.from_variant(variant)
    except ValueError as exc:
        raise VariantLocalizationError(
            "variant does not satisfy PR16 localization bounds"
        ) from exc


def localized_variant_digest(
    variant: Variant | LocalizedVariantSnapshot,
) -> str:
    """Hash accepted localized metadata without hashing shared project media/timeline."""

    snapshot = (
        variant
        if isinstance(variant, LocalizedVariantSnapshot)
        else localized_variant_snapshot(variant)
    )
    return _canonical_digest(snapshot)


def localized_font(variant: Variant | None) -> str | None:
    """Return the reserved portable font-family token for localized text, if selected."""

    if variant is None:
        return None
    value = variant.style_overrides.get(FONT_STYLE_OVERRIDE_KEY)
    return None if value is None else _font_token(value)


def apply_localized_text_style(
    properties: Mapping[str, JsonValue],
    *,
    variant: Variant | None,
    variant_field: str | None,
) -> Mapping[str, JsonValue]:
    """Apply PR16 font intent only to one explicitly variant-bound text component."""

    if variant is None or variant_field is None:
        return properties
    font = localized_font(variant)
    if font is None:
        return properties
    updated = dict(properties)
    updated[FONT_STYLE_OVERRIDE_KEY] = font
    return updated


def _localized_overlay(overlay: Overlay, variant: Variant) -> Overlay:
    properties = apply_localized_text_style(
        overlay.properties,
        variant=variant,
        variant_field=overlay.variant_field,
    )
    if properties == overlay.properties:
        return overlay
    return overlay.validated_copy(update={"properties": dict(properties)})


def _localized_scene(scene: Scene, variant: Variant) -> Scene:
    overlays = tuple(_localized_overlay(overlay, variant) for overlay in scene.overlays)
    if overlays == scene.overlays:
        return scene
    return scene.validated_copy(update={"overlays": overlays})


def _localized_project(project: Project, variant: Variant) -> Project:
    overlays = tuple(_localized_overlay(overlay, variant) for overlay in project.overlays)
    scenes = tuple(_localized_scene(scene, variant) for scene in project.scenes)
    if overlays == project.overlays and scenes == project.scenes:
        return project
    return project.validated_copy(update={"overlays": overlays, "scenes": scenes})


def _localized_template(
    template: "ResolvedTemplate | None",
    variant: Variant,
) -> "ResolvedTemplate | None":
    if template is None:
        return None
    overlays = tuple(_localized_overlay(overlay, variant) for overlay in template.overlays)
    scenes = (
        None
        if template.scenes is None
        else tuple(_localized_scene(scene, variant) for scene in template.scenes)
    )
    if overlays == template.overlays and scenes == template.scenes:
        return template
    return template.validated_copy(update={"overlays": overlays, "scenes": scenes})


def _project_variant(project: Project, variant_id: str) -> Variant:
    for variant in project.variants:
        if variant.variant_id == variant_id:
            return variant
    raise VariantLocalizationError(f"unknown language variant: {variant_id}")


def compile_language_variant(
    project: Project,
    assets: "AssetSource",
    *,
    variant_id: str,
    profile_id: str | None = None,
    template: "ResolvedTemplate | None" = None,
) -> CompiledLanguageVariant:
    """Compile one language variant and freeze the exact metadata used for that plan.

    The project/template copies are ephemeral: only variant-bound overlay properties are
    decorated with the selected portable font token. Scene IDs, media references,
    timing, provenance, profiles, and canonical project storage remain shared.
    """

    from content_forge.timeline import compile_timeline

    variant = _project_variant(project, variant_id)
    snapshot = localized_variant_snapshot(variant)
    localized_project = _localized_project(project, variant)
    localized_template = _localized_template(template, variant)
    plan = compile_timeline(
        localized_project,
        assets,
        profile_id=profile_id,
        variant_id=variant.variant_id,
        template=localized_template,
    )
    return CompiledLanguageVariant(plan=plan, localized_variant=snapshot)


def build_language_variant(
    *,
    language: str,
    variant_id: str | None = None,
    hook: str | None = None,
    subtitle: str | None = None,
    title: str | None = None,
    description: str | None = None,
    hashtags: tuple[str, ...] = (),
    font: str | None = None,
    text_overrides: Mapping[str, str] | None = None,
    style_overrides: Mapping[str, JsonValue] | None = None,
) -> Variant:
    """Build one canonical Variant while keeping localized data above the timeline.

    `subtitle` maps to the reserved `text_overrides["subtitle"]` key. `font` maps to the
    reserved `style_overrides["font"]` key. Conflicting explicit and mapping values fail
    closed rather than silently choosing one.
    """

    texts = {} if text_overrides is None else dict(text_overrides)
    styles = {} if style_overrides is None else dict(style_overrides)

    if subtitle is not None:
        existing = texts.get(SUBTITLE_TEXT_OVERRIDE_KEY)
        if existing is not None and existing != subtitle:
            raise VariantLocalizationError(
                "subtitle conflicts with text_overrides['subtitle']"
            )
        texts[SUBTITLE_TEXT_OVERRIDE_KEY] = subtitle

    existing_font = styles.get(FONT_STYLE_OVERRIDE_KEY)
    if existing_font is not None:
        styles[FONT_STYLE_OVERRIDE_KEY] = _font_token(existing_font)
    if font is not None:
        normalized_font = _font_token(font)
        existing_font = styles.get(FONT_STYLE_OVERRIDE_KEY)
        if existing_font is not None and existing_font != normalized_font:
            raise VariantLocalizationError(
                "font conflicts with style_overrides['font']"
            )
        styles[FONT_STYLE_OVERRIDE_KEY] = normalized_font

    payload: dict[str, object] = {
        "language": language,
        "hook": hook,
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "text_overrides": texts,
        "style_overrides": styles,
    }
    if variant_id is not None:
        payload["variant_id"] = variant_id
    variant = Variant.model_validate(payload)
    snapshot = localized_variant_snapshot(variant)

    return variant.validated_copy(
        update={
            "language": snapshot.language,
            "hashtags": snapshot.hashtags,
            "text_overrides": dict(snapshot.text_overrides),
            "style_overrides": dict(snapshot.style_overrides),
        }
    )


def variant_render_cache_identity(
    compiled: CompiledLanguageVariant,
    *,
    purpose: Literal["preview", "final"],
) -> VariantRenderCacheIdentity:
    """Build cache identity from one atomically compiled plan/metadata pair."""

    from content_forge.timeline import render_plan_digest

    plan = compiled.plan
    snapshot = compiled.localized_variant
    profile_purpose = plan.output_profile.properties.get("purpose")
    if profile_purpose is not None and profile_purpose != purpose:
        raise VariantCacheIdentityError(
            "cache purpose does not match output profile purpose"
        )

    return VariantRenderCacheIdentity(
        purpose=purpose,
        project_id=plan.project_id,
        variant_id=snapshot.variant_id,
        variant_language=snapshot.language,
        profile_id=plan.output_profile.profile_id,
        template_id=plan.template_id,
        template_version=plan.template_version,
        render_plan_digest=render_plan_digest(plan),
        localized_variant_digest=localized_variant_digest(snapshot),
    )


def variant_render_cache_key(
    compiled: CompiledLanguageVariant,
    *,
    purpose: Literal["preview", "final"],
) -> str:
    """Return the stable PR16 variant-specific preview/final semantic cache key."""

    return _canonical_digest(
        variant_render_cache_identity(compiled, purpose=purpose)
    )


__all__ = [
    "CompiledLanguageVariant",
    "FONT_STYLE_OVERRIDE_KEY",
    "LOCALIZED_VARIANT_CONTRACT_VERSION",
    "LocalizedVariantSnapshot",
    "SUBTITLE_TEXT_OVERRIDE_KEY",
    "VARIANT_RENDER_CACHE_KEY_VERSION",
    "VariantCacheIdentityError",
    "VariantLocalizationError",
    "VariantRenderCacheIdentity",
    "apply_localized_text_style",
    "build_language_variant",
    "compile_language_variant",
    "localized_font",
    "localized_variant_digest",
    "localized_variant_snapshot",
    "variant_render_cache_identity",
    "variant_render_cache_key",
]
