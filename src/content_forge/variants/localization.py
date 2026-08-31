"""PR16 language-variant metadata, font intent, and cache identity contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from content_forge.core import EntityKind, RegistryKey, Variant, require_entity_id
from content_forge.core.models import FrozenModel, LanguageTag
from content_forge.timeline import RenderPlan, render_plan_digest

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


class VariantLocalizationError(ValueError):
    """Localized variant data cannot satisfy the PR16 language-variant contract."""


class VariantCacheIdentityError(VariantLocalizationError):
    """A render plan and localized variant cannot form one cache identity."""


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

    @model_validator(mode="after")
    def validate_localized_contract(self) -> Self:
        if len(self.hashtags) != len(set(self.hashtags)):
            raise ValueError("localized hashtags must be unique after normalization")
        if len(self.text_overrides) > _MAX_TEXT_OVERRIDES:
            raise ValueError("localized text overrides exceed supported maximum")
        if len(self.style_overrides) > _MAX_STYLE_OVERRIDES:
            raise ValueError("localized style overrides exceed supported maximum")

        font = self.style_overrides.get(FONT_STYLE_OVERRIDE_KEY)
        if font is not None:
            # Reuse the public font-token validation contract for the reserved style key.
            _font_token(font)
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


def _font_token(value: object) -> str:
    if not isinstance(value, str):
        raise VariantLocalizationError("variant font style override must be a string")
    try:
        # Pydantic validates the exact same public token contract used by snapshots.
        class _FontValue(FrozenModel):
            font: FontFamilyToken

        return _FontValue(font=value).font
    except ValueError as exc:
        raise VariantLocalizationError(
            "variant font must be a portable family token, not a filesystem path"
        ) from exc


def localized_variant_snapshot(variant: Variant) -> LocalizedVariantSnapshot:
    """Materialize the bounded PR16 metadata contract from one canonical Variant."""

    try:
        return LocalizedVariantSnapshot.from_variant(variant)
    except ValueError as exc:
        raise VariantLocalizationError("variant does not satisfy PR16 localization bounds") from exc


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
    """Apply PR16 variant style intent only to text bound to a variant field.

    Explicit localized font selection intentionally overrides the template's generic
    text font for that localized field. Other style overrides remain metadata until a
    future version gives them renderer-independent semantics.
    """

    if variant is None or variant_field is None:
        return properties
    font = localized_font(variant)
    if font is None:
        return properties
    updated = dict(properties)
    updated[FONT_STYLE_OVERRIDE_KEY] = font
    return updated


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
    """Build one canonical Variant while keeping localized text/style above the timeline.

    `subtitle` maps to the reserved `text_overrides["subtitle"]` key. `font` maps to the
    reserved `style_overrides["font"]` key. Conflicting explicit and mapping values fail
    closed rather than silently choosing one.
    """

    texts = {} if text_overrides is None else dict(text_overrides)
    styles = {} if style_overrides is None else dict(style_overrides)

    if subtitle is not None:
        existing = texts.get(SUBTITLE_TEXT_OVERRIDE_KEY)
        if existing is not None and existing != subtitle:
            raise VariantLocalizationError("subtitle conflicts with text_overrides['subtitle']")
        texts[SUBTITLE_TEXT_OVERRIDE_KEY] = subtitle

    if font is not None:
        normalized_font = _font_token(font)
        existing = styles.get(FONT_STYLE_OVERRIDE_KEY)
        if existing is not None and existing != normalized_font:
            raise VariantLocalizationError("font conflicts with style_overrides['font']")
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

    # Write normalized bounded strings back into the canonical Variant so snapshot and
    # stored project metadata share one exact representation.
    return variant.validated_copy(
        update={
            "language": snapshot.language,
            "hashtags": snapshot.hashtags,
            "text_overrides": dict(snapshot.text_overrides),
            "style_overrides": dict(snapshot.style_overrides),
        }
    )


def variant_render_cache_identity(
    plan: RenderPlan,
    variant: Variant,
    *,
    purpose: Literal["preview", "final"],
) -> VariantRenderCacheIdentity:
    """Build a cache identity from a frozen plan plus the accepted localized snapshot."""

    if plan.project_id is None:
        raise VariantCacheIdentityError("render plan has no project identity")
    if plan.variant_id != variant.variant_id:
        raise VariantCacheIdentityError("render plan variant_id does not match variant")
    if plan.variant_language != variant.language:
        raise VariantCacheIdentityError("render plan variant language does not match variant")

    snapshot = localized_variant_snapshot(variant)
    return VariantRenderCacheIdentity(
        purpose=purpose,
        project_id=plan.project_id,
        variant_id=variant.variant_id,
        variant_language=variant.language,
        profile_id=plan.output_profile.profile_id,
        template_id=plan.template_id,
        template_version=plan.template_version,
        render_plan_digest=render_plan_digest(plan),
        localized_variant_digest=localized_variant_digest(snapshot),
    )


def variant_render_cache_key(
    plan: RenderPlan,
    variant: Variant,
    *,
    purpose: Literal["preview", "final"],
) -> str:
    """Return the stable PR16 variant-specific preview/final semantic cache key."""

    return _canonical_digest(
        variant_render_cache_identity(plan, variant, purpose=purpose)
    )


__all__ = [
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
    "localized_font",
    "localized_variant_digest",
    "localized_variant_snapshot",
    "variant_render_cache_identity",
    "variant_render_cache_key",
]
