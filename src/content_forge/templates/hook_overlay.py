"""First concrete presentation template: full-screen media with a top hook."""

from __future__ import annotations

import hashlib
import json
import math
import textwrap
import unicodedata
from collections.abc import Mapping
from typing import Protocol, Self

from pydantic import Field, model_validator

from content_forge.core import (
    Asset,
    AudioTrack,
    EntityKind,
    FitMode,
    MediaType,
    NormalizedRect,
    OutputProfile,
    Overlay,
    Project,
    Scene,
    Variant,
)
from content_forge.core.models import FULL_CANVAS, FrozenModel
from content_forge.timeline import RenderPlan, ResolvedTemplate, compile_timeline

HOOK_OVERLAY_TEMPLATE_ID = "hook_overlay"
HOOK_OVERLAY_TEMPLATE_VERSION = "1.0"
HOOK_OVERLAY_FONT_FAMILY = "sans-serif"
_DEFAULT_HOOK_REGION = NormalizedRect(x=0.06, y=0.06, width=0.80, height=0.19)
_STYLE_VALUE_PATTERN = r"^[A-Za-z0-9#@._+-]+$"
_STYLE_PREFIX = "hook_overlay."


class HookOverlayTemplateError(ValueError):
    """Raised when a project cannot be resolved safely as hook_overlay."""


class HookOverlayAssetResolver(Protocol):
    def get_asset(self, asset_id: str) -> Asset | None: ...


HookOverlayAssetSource = Mapping[str, Asset] | HookOverlayAssetResolver


class HookOverlayConfig(FrozenModel):
    """Renderer-independent policy for the first hook presentation template."""

    hook_region: NormalizedRect = _DEFAULT_HOOK_REGION
    source_fit: FitMode = FitMode.COVER
    font_size_ratio: float = Field(default=0.058, gt=0.0, le=0.2)
    border_width_ratio: float = Field(default=0.0037, ge=0.0, le=0.03)
    # The simple drawtext path cannot measure the exact host font before compilation.
    # Budget at least one full em per code point so wide Latin glyphs such as W/M cannot
    # escape the declared hook region. Callers may only make this more conservative.
    max_glyph_width_em: float = Field(default=1.0, ge=1.0, le=2.0)
    # Keep a conservative minimum line-height budget as long as PR6 relies on drawtext
    # rather than measured/rasterized typography. Callers may only increase it.
    line_height_em: float = Field(default=1.35, ge=1.35, le=2.0)
    max_lines: int = Field(default=4, ge=1, le=8)
    font_color: str = Field(default="white", pattern=_STYLE_VALUE_PATTERN)
    border_color: str = Field(default="black", pattern=_STYLE_VALUE_PATTERN)
    box: bool = True
    box_color: str = Field(default="black@0.55", pattern=_STYLE_VALUE_PATTERN)
    z_index: int = Field(default=100, ge=-10000, le=10000)
    original_audio: bool = True
    original_audio_gain_db: float = Field(default=0.0, ge=-120.0, le=24.0)

    @model_validator(mode="after")
    def validate_source_fit(self) -> Self:
        if self.source_fit is FitMode.STRETCH:
            raise ValueError("hook_overlay does not permit aspect-ratio-distorting stretch")
        return self


_ALLOWED_STYLE_OVERRIDES = frozenset(HookOverlayConfig.model_fields)


def _derived_id(kind: EntityKind, *parts: str) -> str:
    payload = json.dumps(
        ["content-forge-derived-id-v1", kind.value, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(payload).hexdigest()[:32]
    return f"cf_{kind.value}_{suffix}"


def _asset_from(source: HookOverlayAssetSource, asset_id: str) -> Asset | None:
    if isinstance(source, Mapping):
        return source.get(asset_id)
    return source.get_asset(asset_id)


def _select_profile(project: Project, profile_id: str | None) -> OutputProfile:
    if profile_id is not None:
        for profile in project.output_profiles:
            if profile.profile_id == profile_id:
                return profile
        raise HookOverlayTemplateError(f"unknown output profile: {profile_id}")
    if len(project.output_profiles) != 1:
        raise HookOverlayTemplateError(
            "profile_id is required unless the project has exactly one output profile"
        )
    return project.output_profiles[0]


def _select_variant(project: Project, variant_id: str | None) -> Variant:
    if variant_id is not None:
        for variant in project.variants:
            if variant.variant_id == variant_id:
                return variant
        raise HookOverlayTemplateError(f"unknown variant: {variant_id}")
    if len(project.variants) != 1:
        raise HookOverlayTemplateError(
            "hook_overlay requires exactly one implicit variant or an explicit variant_id"
        )
    return project.variants[0]


def _validate_template_ref(project: Project) -> None:
    if project.template is None:
        raise HookOverlayTemplateError("project has no template reference")
    if project.template.template_id != HOOK_OVERLAY_TEMPLATE_ID:
        raise HookOverlayTemplateError(
            f"project template must be {HOOK_OVERLAY_TEMPLATE_ID!r}"
        )
    if project.template.version != HOOK_OVERLAY_TEMPLATE_VERSION:
        raise HookOverlayTemplateError(
            "project hook_overlay template version does not match the built-in resolver"
        )


def _rectangles_overlap(
    left: NormalizedRect,
    right: NormalizedRect,
    *,
    pad_x: float = 0.0,
    pad_y: float = 0.0,
) -> bool:
    return (
        left.x - pad_x < right.x + right.width
        and right.x < left.x + left.width + pad_x
        and left.y - pad_y < right.y + right.height
        and right.y < left.y + left.height + pad_y
    )


def _validate_profile(
    profile: OutputProfile,
    config: HookOverlayConfig,
    *,
    border_width: int,
) -> None:
    if profile.width >= profile.height:
        raise HookOverlayTemplateError("hook_overlay requires a vertical output profile")

    # drawtext outline can extend outside the nominal placement. Keep the same
    # conservative reserve used by the horizontal/vertical layout budgets and protect
    # both the output canvas and safe zones against that expanded visual footprint.
    decoration_padding = border_width * (2 if config.box else 1)
    pad_x = decoration_padding / profile.width
    pad_y = decoration_padding / profile.height
    if (
        config.hook_region.x - pad_x < 0.0
        or config.hook_region.y - pad_y < 0.0
        or config.hook_region.x + config.hook_region.width + pad_x > 1.0
        or config.hook_region.y + config.hook_region.height + pad_y > 1.0
    ):
        raise HookOverlayTemplateError(
            "hook region or text decoration exceeds output canvas"
        )

    for safe_zone in profile.safe_zones:
        if _rectangles_overlap(
            config.hook_region,
            safe_zone.rect,
            pad_x=pad_x,
            pad_y=pad_y,
        ):
            raise HookOverlayTemplateError(
                f"hook region or text decoration overlaps protected output safe zone: "
                f"{safe_zone.name}"
            )


def _config_for_variant(base: HookOverlayConfig, variant: Variant) -> HookOverlayConfig:
    updates: dict[str, object] = {}
    for key, value in variant.style_overrides.items():
        if not key.startswith(_STYLE_PREFIX):
            continue
        field_name = key[len(_STYLE_PREFIX) :]
        if field_name not in _ALLOWED_STYLE_OVERRIDES:
            raise HookOverlayTemplateError(
                f"unknown hook_overlay style override: {field_name}"
            )
        updates[field_name] = value
    if not updates:
        return base
    try:
        return base.validated_copy(update=updates)
    except ValueError as exc:
        raise HookOverlayTemplateError(f"invalid hook_overlay style override: {exc}") from exc


def _hook_text(variant: Variant) -> str:
    value = variant.text_overrides.get("hook")
    if value is None:
        value = variant.hook
    if value is None or not value.strip():
        raise HookOverlayTemplateError("selected variant has no non-empty hook text")
    if "\x00" in value:
        raise HookOverlayTemplateError("hook text cannot contain NUL")
    return value


def _simple_drawtext_character_supported(character: str) -> bool:
    """Return whether v1's host-font drawtext path has bounded glyph expectations.

    PR6 deliberately supports the common Latin/Greek/Cyrillic writing systems plus
    combining marks and ordinary punctuation. CJK, emoji and other wide/specialized
    scripts need an explicitly pinned font/raster text pipeline; accepting them here
    would make glyph coverage depend on the host and could silently render tofu boxes.
    """

    if character in "\n\r\t":
        return True
    codepoint = ord(character)
    if codepoint < 0x20 or codepoint == 0x7F:
        return False
    if 0x20 <= codepoint <= 0x024F:  # ASCII, Latin-1, Latin Extended A/B.
        return True
    if 0x0300 <= codepoint <= 0x036F:  # Combining diacritics.
        return True
    if 0x0370 <= codepoint <= 0x03FF:  # Greek and Coptic.
        return True
    if 0x0400 <= codepoint <= 0x052F:  # Cyrillic + supplements.
        return True
    if 0x2000 <= codepoint <= 0x206F:  # General punctuation.
        return True
    if 0x20A0 <= codepoint <= 0x20CF:  # Currency symbols.
        return True
    return False


def _validate_hook_character_coverage(text: str) -> None:
    unsupported = [
        character for character in text if not _simple_drawtext_character_supported(character)
    ]
    if not unsupported:
        return
    character = unsupported[0]
    name = unicodedata.name(character, "UNNAMED")
    raise HookOverlayTemplateError(
        "hook contains a glyph outside hook_overlay v1 simple drawtext coverage: "
        f"U+{ord(character):04X} {name}; use a font-backed text pipeline for CJK/emoji/"
        "other specialized scripts"
    )


def _wrap_hook(text: str, config: HookOverlayConfig) -> tuple[str, int, int]:
    _validate_hook_character_coverage(text)

    # Wrapping is a semantic template decision, so derive it entirely from normalized
    # ratios. Preview/final profiles therefore receive identical line breaks even when
    # their integer pixel font sizes round differently.
    decoration_width_ratio = config.border_width_ratio * (4 if config.box else 2)
    available_width_ratio = config.hook_region.width - decoration_width_ratio
    glyph_width_ratio = config.font_size_ratio * config.max_glyph_width_em
    width = (
        int(math.floor(available_width_ratio / glyph_width_ratio))
        if available_width_ratio > 0.0
        else 0
    )
    if width < 1:
        raise HookOverlayTemplateError(
            "hook region is too narrow for one conservative glyph"
        )

    wrapper = textwrap.TextWrapper(
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=True,
        drop_whitespace=True,
    )
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph.strip():
            continue
        lines.extend(wrapper.wrap(paragraph.strip()))
    if not lines:
        raise HookOverlayTemplateError("hook text became empty after normalization")
    if len(lines) > config.max_lines:
        raise HookOverlayTemplateError(
            f"hook requires {len(lines)} lines but template allows {config.max_lines}"
        )
    return "\n".join(lines), width, len(lines)


def _scaled_integer(value: float, scale: int, *, minimum: int) -> int:
    return max(minimum, int(math.floor(value * scale + 0.5)))


def _validate_horizontal_layout(
    profile: OutputProfile,
    config: HookOverlayConfig,
    *,
    wrapped_hook: str,
    font_size: int,
    border_width: int,
) -> tuple[int, int, int]:
    """Reject rounded pixel layouts that exceed the semantic horizontal budget."""

    region_width = max(1, int(math.floor(config.hook_region.width * profile.width)))
    decoration_width = border_width * (4 if config.box else 2)
    available_width = region_width - decoration_width
    longest_line = max(len(line) for line in wrapped_hook.split("\n"))
    required_width = int(
        math.ceil(
            longest_line * font_size * config.max_glyph_width_em + decoration_width
        )
    )
    if available_width <= 0 or required_width > region_width:
        raise HookOverlayTemplateError(
            "hook text exceeds hook region width after profile pixel scaling: "
            f"requires {required_width}px but only {region_width}px is available"
        )
    return region_width, available_width, required_width


def _validate_vertical_layout(
    profile: OutputProfile,
    config: HookOverlayConfig,
    *,
    line_count: int,
    font_size: int,
    border_width: int,
) -> tuple[int, int, int]:
    """Reject drawtext layouts that can exceed the declared hook region vertically."""

    available_height = max(1, int(math.floor(config.hook_region.height * profile.height)))
    line_height = max(font_size, int(math.ceil(font_size * config.line_height_em)))
    # Text outline expands on both vertical sides. Box rendering itself has no explicit
    # boxborderw in PR6, but reserving one extra outline-width per side keeps the simple
    # path fail-closed across FFmpeg/fontconfig differences.
    decoration_height = border_width * (4 if config.box else 2)
    required_height = line_count * line_height + decoration_height
    if required_height > available_height:
        raise HookOverlayTemplateError(
            "hook text exceeds hook region height: "
            f"requires {required_height}px but only {available_height}px is available"
        )
    return available_height, line_height, required_height


def _generated_id_collides(project: Project, generated_id: str) -> bool:
    for overlay in project.overlays:
        if overlay.overlay_id == generated_id:
            return True
    for track in project.audio_tracks:
        if track.audio_track_id == generated_id:
            return True
    for scene in project.scenes:
        if any(item.overlay_id == generated_id for item in scene.overlays):
            return True
        if any(item.audio_track_id == generated_id for item in scene.audio_tracks):
            return True
    return False


def _resolve_selected(
    project: Project,
    assets: HookOverlayAssetSource,
    profile: OutputProfile,
    variant: Variant,
    base_config: HookOverlayConfig,
) -> ResolvedTemplate:
    _validate_template_ref(project)
    config = _config_for_variant(base_config, variant)
    if not project.scenes:
        raise HookOverlayTemplateError("hook_overlay requires at least one source scene")

    # Keep typography proportional to output width. The previous absolute 12px floor
    # made preview/final wrapping diverge for small-but-valid font_size_ratio values.
    font_size = _scaled_integer(config.font_size_ratio, profile.width, minimum=1)
    border_width = _scaled_integer(
        config.border_width_ratio,
        profile.width,
        minimum=0,
    )
    _validate_profile(profile, config, border_width=border_width)
    wrapped_hook, wrap_width, line_count = _wrap_hook(_hook_text(variant), config)
    region_width, available_width, required_width = _validate_horizontal_layout(
        profile,
        config,
        wrapped_hook=wrapped_hook,
        font_size=font_size,
        border_width=border_width,
    )

    hook_overlay_id = _derived_id(
        EntityKind.OVERLAY,
        project.project_id,
        HOOK_OVERLAY_TEMPLATE_ID,
        HOOK_OVERLAY_TEMPLATE_VERSION,
        "hook",
    )
    if _generated_id_collides(project, hook_overlay_id):
        raise HookOverlayTemplateError("generated hook overlay ID collides with project state")

    resolved_scenes: list[Scene] = []
    auto_audio_count = 0
    for scene in project.scenes:
        if scene.media is None:
            raise HookOverlayTemplateError(
                f"hook_overlay scene has no media asset: {scene.scene_id}"
            )
        asset = _asset_from(assets, scene.media.asset_id)
        if asset is None:
            raise HookOverlayTemplateError(
                f"hook_overlay cannot resolve scene asset: {scene.media.asset_id}"
            )
        if asset.asset_id != scene.media.asset_id:
            raise HookOverlayTemplateError("asset resolver returned mismatched asset identity")
        if asset.media_type not in {MediaType.VIDEO, MediaType.IMAGE}:
            raise HookOverlayTemplateError(
                f"hook_overlay source must be image or video: {asset.asset_id}"
            )

        tracks = list(scene.audio_tracks)
        if config.original_audio and asset.media_type is MediaType.VIDEO:
            already_has_original = any(track.track_type == "original" for track in tracks)
            if not already_has_original:
                if asset.has_audio is None:
                    raise HookOverlayTemplateError(
                        f"video audio metadata is unknown: {asset.asset_id}"
                    )
                if asset.has_audio:
                    audio_id = _derived_id(
                        EntityKind.AUDIO,
                        project.project_id,
                        HOOK_OVERLAY_TEMPLATE_ID,
                        HOOK_OVERLAY_TEMPLATE_VERSION,
                        scene.scene_id,
                        "original-audio",
                    )
                    if _generated_id_collides(project, audio_id):
                        raise HookOverlayTemplateError(
                            "generated original-audio ID collides with project state"
                        )
                    tracks.append(
                        AudioTrack(
                            audio_track_id=audio_id,
                            track_type="original",
                            gain_db=config.original_audio_gain_db,
                        )
                    )
                    auto_audio_count += 1

        resolved_scenes.append(
            scene.validated_copy(
                update={
                    "placement": FULL_CANVAS,
                    "fit_mode": config.source_fit,
                    "audio_tracks": tuple(tracks),
                }
            )
        )

    region_height, line_height, required_height = _validate_vertical_layout(
        profile,
        config,
        line_count=line_count,
        font_size=font_size,
        border_width=border_width,
    )
    hook_overlay = Overlay(
        overlay_id=hook_overlay_id,
        component_type="text",
        start_seconds=0.0,
        duration_seconds=None,
        placement=config.hook_region,
        z_index=config.z_index,
        text=wrapped_hook,
        properties={
            "font": HOOK_OVERLAY_FONT_FAMILY,
            "font_size": font_size,
            "border_width": border_width,
            "font_color": config.font_color,
            "border_color": config.border_color,
            "box": config.box,
            "box_color": config.box_color,
        },
    )

    return ResolvedTemplate(
        template_id=HOOK_OVERLAY_TEMPLATE_ID,
        version=HOOK_OVERLAY_TEMPLATE_VERSION,
        scenes=tuple(resolved_scenes),
        overlays=(hook_overlay,),
        properties={
            "resolved_profile_id": profile.profile_id,
            "resolved_variant_id": variant.variant_id,
            "hook_wrap_width_chars": wrap_width,
            "hook_line_count": line_count,
            "font_family": HOOK_OVERLAY_FONT_FAMILY,
            "font_size_pixels": font_size,
            "border_width_pixels": border_width,
            "hook_region_width_pixels": region_width,
            "hook_available_width_pixels": available_width,
            "hook_required_width_pixels": required_width,
            "line_height_em": config.line_height_em,
            "line_height_pixels": line_height,
            "hook_region_height_pixels": region_height,
            "hook_required_height_pixels": required_height,
            "source_fit": config.source_fit.value,
            "auto_original_audio_tracks": auto_audio_count,
        },
    )


def resolve_hook_overlay(
    project: Project,
    assets: HookOverlayAssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
    config: HookOverlayConfig | None = None,
) -> ResolvedTemplate:
    """Resolve hook_overlay into ordinary Scene/Overlay/AudioTrack primitives."""

    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id)
    return _resolve_selected(project, assets, profile, variant, config or HookOverlayConfig())


def compile_hook_overlay(
    project: Project,
    assets: HookOverlayAssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
    config: HookOverlayConfig | None = None,
) -> RenderPlan:
    """Resolve and compile hook_overlay with profile/variant identity kept together."""

    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id)
    resolved = _resolve_selected(
        project,
        assets,
        profile,
        variant,
        config or HookOverlayConfig(),
    )
    return compile_timeline(
        project,
        assets,
        profile_id=profile.profile_id,
        variant_id=variant.variant_id,
        template=resolved,
    )
