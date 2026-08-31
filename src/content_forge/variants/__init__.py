"""Language-variant metadata and cache identity contracts."""

from .localization import (
    FONT_STYLE_OVERRIDE_KEY,
    LOCALIZED_VARIANT_CONTRACT_VERSION,
    SUBTITLE_TEXT_OVERRIDE_KEY,
    VARIANT_RENDER_CACHE_KEY_VERSION,
    LocalizedVariantSnapshot,
    VariantCacheIdentityError,
    VariantLocalizationError,
    VariantRenderCacheIdentity,
    apply_localized_text_style,
    build_language_variant,
    localized_font,
    localized_variant_digest,
    localized_variant_snapshot,
    variant_render_cache_identity,
    variant_render_cache_key,
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
