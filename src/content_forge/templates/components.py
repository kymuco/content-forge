"""PR13 reusable renderer-independent overlay, motion, and transition components."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    MotionSpec,
    NormalizedPoint,
    NormalizedRect,
    OutputProfile,
    Overlay,
    Project,
    TransitionSpec,
    require_entity_id,
)

from .contracts import ComponentDefinition
from .hook_overlay import (
    HOOK_OVERLAY_FONT_FAMILY,
    HookOverlayConfig,
    HookOverlayTemplateError,
    _layout_metrics_for_profile,
    _wrap_hook,
)

PR13_COMPONENT_VERSION = "1.0"
ARTIST_CREDIT_COMPONENT_ID = "artist_credit"
COMMENT_CARD_COMPONENT_ID = "comment_card"
REACTION_COMPONENT_ID = "reaction"
AVATAR_COMPONENT_ID = "avatar"
WATERMARK_COMPONENT_ID = "watermark"
KEN_BURNS_COMPONENT_ID = "ken_burns"
PAN_COMPONENT_ID = "pan"
CROP_REVEAL_COMPONENT_ID = "crop_reveal"
BLUR_REVEAL_COMPONENT_ID = "blur_reveal"
TRANSITION_COMPONENT_ID = "transition"

ARTIST_CREDIT_COMPONENT = ComponentDefinition(
    component_id=ARTIST_CREDIT_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="overlay",
    accepts_text=True,
    description="Bounded source-credit text with deterministic wrapping and overflow checks.",
)
COMMENT_CARD_COMPONENT = ComponentDefinition(
    component_id=COMMENT_CARD_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="overlay",
    accepts_text=True,
    accepts_asset=True,
    description="Bounded provenance-explicit comment card; Avatar composes separately.",
)
REACTION_COMPONENT = ComponentDefinition(
    component_id=REACTION_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="overlay",
    accepts_asset=True,
    property_defaults={"loop": False},
    description="Reusable image/video reaction overlay with explicit loop intent.",
)
AVATAR_COMPONENT = ComponentDefinition(
    component_id=AVATAR_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="overlay",
    accepts_asset=True,
    description="Aspect-safe reusable avatar/profile image overlay.",
)
WATERMARK_COMPONENT = ComponentDefinition(
    component_id=WATERMARK_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="overlay",
    accepts_text=True,
    accepts_asset=True,
    description="Reusable bounded text or image watermark overlay.",
)
KEN_BURNS_COMPONENT = ComponentDefinition(
    component_id=KEN_BURNS_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="motion",
    property_defaults={"start_zoom": 1.0, "end_zoom": 1.08},
    description="Ken Burns slow zoom resolved to canonical slow_zoom motion.",
)
PAN_COMPONENT = ComponentDefinition(
    component_id=PAN_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="motion",
    property_defaults={"zoom": 1.12},
    description="Aspect-safe crop-window pan between normalized focus points.",
)
CROP_REVEAL_COMPONENT = ComponentDefinition(
    component_id=CROP_REVEAL_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="motion",
    property_defaults={"start_zoom": 1.3, "end_zoom": 1.0},
    description="Progressive crop reveal from a tight crop toward a wider source region.",
)
BLUR_REVEAL_COMPONENT = ComponentDefinition(
    component_id=BLUR_REVEAL_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="motion",
    required_properties=("reveal_duration_seconds",),
    description="Timed blurred-to-sharp reveal over an ordinary fitted scene.",
)
TRANSITION_COMPONENT = ComponentDefinition(
    component_id=TRANSITION_COMPONENT_ID,
    version=PR13_COMPONENT_VERSION,
    output_kind="transition",
    required_properties=("transition_type",),
    property_defaults={"duration_seconds": 0.15},
    description="Validated wrapper around the generic FFmpeg-compatible transition set.",
)
PR13_COMPONENTS = (
    ARTIST_CREDIT_COMPONENT,
    COMMENT_CARD_COMPONENT,
    REACTION_COMPONENT,
    AVATAR_COMPONENT,
    WATERMARK_COMPONENT,
    KEN_BURNS_COMPONENT,
    PAN_COMPONENT,
    CROP_REVEAL_COMPONENT,
    BLUR_REVEAL_COMPONENT,
    TRANSITION_COMPONENT,
)

_SIMPLE_TRANSITIONS = frozenset(
    {
        "crossfade", "fade", "fadeblack", "fadewhite",
        "wipeleft", "wiperight", "wipeup", "wipedown",
        "slideleft", "slideright", "slideup", "slidedown",
    }
)


class ComponentRuntimeError(ValueError):
    """Raised when a reusable component cannot be resolved safely."""


def _overlay_id(project: Project, component_id: str, instance_key: str) -> str:
    payload = json.dumps(
        ["content-forge-component-overlay-v1", project.project_id, component_id,
         PR13_COMPONENT_VERSION, instance_key],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    value = f"cf_{EntityKind.OVERLAY.value}_{hashlib.sha256(payload).hexdigest()[:32]}"
    existing = list(project.overlays)
    for scene in project.scenes:
        existing.extend(scene.overlays)
    if any(item.overlay_id == value for item in existing):
        raise ComponentRuntimeError(f"generated {component_id} overlay ID collides with project state")
    return value


def _require_profile(project: Project, profile: OutputProfile) -> None:
    if not any(item.profile_id == profile.profile_id for item in project.output_profiles):
        raise ComponentRuntimeError("selected output profile is not part of the project")


def _text(value: str, *, label: str, maximum: int) -> str:
    if "\x00" in value:
        raise ComponentRuntimeError(f"{label} cannot contain NUL")
    value = value.strip()
    if not value:
        raise ComponentRuntimeError(f"{label} must be non-empty")
    if len(value) > maximum:
        raise ComponentRuntimeError(f"{label} is too long")
    return value


def bounded_text_overlay(
    project: Project,
    profile: OutputProfile,
    *,
    component_type: str,
    instance_key: str,
    text: str,
    placement: NormalizedRect,
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
    z_index: int = 100,
    font_size_ratio: float = 0.03,
    border_width_ratio: float = 0.001,
    max_lines: int = 3,
    font_color: str = "white",
    border_color: str = "black",
    box: bool = True,
    box_color: str = "black@0.55",
) -> Overlay:
    """Resolve text once and preflight the same wrapping against every output profile."""

    _require_profile(project, profile)
    value = _text(text, label=f"{component_type} text", maximum=4096)
    config = HookOverlayConfig(
        hook_region=placement,
        font_size_ratio=font_size_ratio,
        border_width_ratio=border_width_ratio,
        max_lines=max_lines,
        font_color=font_color,
        border_color=border_color,
        box=box,
        box_color=box_color,
    )
    try:
        wrapped, wrap_width, line_count = _wrap_hook(value, config)
        selected = None
        for candidate in project.output_profiles:
            metrics = _layout_metrics_for_profile(
                candidate, config, wrapped_hook=wrapped, line_count=line_count
            )
            if candidate.profile_id == profile.profile_id:
                selected = metrics
    except HookOverlayTemplateError as exc:
        raise ComponentRuntimeError(
            f"{component_type} text overflows its bounded region: {exc}"
        ) from exc
    if selected is None:
        raise ComponentRuntimeError("selected output profile is missing from project outputs")
    (
        font_size, border_width, region_width, _, required_width,
        region_height, _, required_height,
    ) = selected
    return Overlay(
        overlay_id=_overlay_id(project, component_type, instance_key),
        component_type=component_type,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        placement=placement,
        z_index=z_index,
        text=wrapped,
        properties={
            "font": HOOK_OVERLAY_FONT_FAMILY,
            "font_size": font_size,
            "border_width": border_width,
            "font_color": font_color,
            "border_color": border_color,
            "box": box,
            "box_color": box_color,
            "layout_wrap_width_chars": wrap_width,
            "layout_line_count": line_count,
            "layout_region_width_pixels": region_width,
            "layout_required_width_pixels": required_width,
            "layout_region_height_pixels": region_height,
            "layout_required_height_pixels": required_height,
        },
    )


def artist_credit_overlay(
    project: Project,
    profile: OutputProfile,
    *,
    credit_text: str,
    placement: NormalizedRect,
    instance_key: str = "credit",
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
    z_index: int = 120,
) -> Overlay:
    return bounded_text_overlay(
        project,
        profile,
        component_type=ARTIST_CREDIT_COMPONENT_ID,
        instance_key=instance_key,
        text=_text(credit_text, label="artist credit", maximum=512),
        placement=placement,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        z_index=z_index,
        font_size_ratio=0.022,
        max_lines=2,
        box_color="black@0.65",
    )


def comment_card_overlay(
    project: Project,
    profile: OutputProfile,
    *,
    display_label: str,
    comment_text: str,
    placement: NormalizedRect,
    instance_key: str = "comment",
    provenance: Literal["synthetic", "source"] = "synthetic",
    source_id: str | None = None,
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
    z_index: int = 140,
) -> Overlay:
    label = _text(display_label, label="comment display label", maximum=128)
    comment = _text(comment_text, label="comment text", maximum=1024)
    if provenance == "source":
        if source_id is None:
            raise ComponentRuntimeError("source comment requires source_id provenance")
        try:
            require_entity_id(source_id, EntityKind.SOURCE)
        except ValueError as exc:
            raise ComponentRuntimeError("comment source_id is not a Content Forge source ID") from exc
    elif source_id is not None:
        raise ComponentRuntimeError("synthetic comment cannot claim a source_id")
    overlay = bounded_text_overlay(
        project,
        profile,
        component_type=COMMENT_CARD_COMPONENT_ID,
        instance_key=instance_key,
        text=f"{label}\n{comment}",
        placement=placement,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        z_index=z_index,
        font_size_ratio=0.028,
        max_lines=6,
        box_color="black@0.78",
    )
    properties = overlay.model_dump(mode="json")["properties"]
    properties["comment_provenance"] = provenance
    if source_id is not None:
        properties["comment_source_id"] = source_id
    return overlay.validated_copy(update={"properties": properties})


def aspect_safe_rect(asset: Asset, profile: OutputProfile, cell: NormalizedRect) -> NormalizedRect:
    if asset.width is None or asset.height is None:
        raise ComponentRuntimeError(
            f"asset dimensions are required for aspect-safe placement: {asset.asset_id}"
        )
    cw = cell.width * profile.width
    ch = cell.height * profile.height
    scale = min(cw / asset.width, ch / asset.height)
    width = asset.width * scale / profile.width
    height = asset.height * scale / profile.height
    return NormalizedRect(
        x=cell.x + (cell.width - width) / 2.0,
        y=cell.y + (cell.height - height) / 2.0,
        width=width,
        height=height,
    )


def _visual(asset: Asset, *, label: str, image_only: bool = False) -> None:
    allowed = {MediaType.IMAGE} if image_only else {MediaType.IMAGE, MediaType.VIDEO}
    if asset.media_type not in allowed:
        raise ComponentRuntimeError(
            f"{label} asset must be {'image' if image_only else 'image or video'}"
        )


def avatar_overlay(
    project: Project,
    profile: OutputProfile,
    *,
    asset: Asset,
    asset_ref: AssetRef,
    cell: NormalizedRect,
    instance_key: str = "avatar",
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
    z_index: int = 130,
) -> Overlay:
    _require_profile(project, profile)
    _visual(asset, label="avatar", image_only=True)
    if asset_ref.asset_id != asset.asset_id:
        raise ComponentRuntimeError("avatar AssetRef does not match the supplied asset")
    return Overlay(
        overlay_id=_overlay_id(project, AVATAR_COMPONENT_ID, instance_key),
        component_type=AVATAR_COMPONENT_ID,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        placement=aspect_safe_rect(asset, profile, cell),
        z_index=z_index,
        asset_ref=asset_ref.validated_copy(update={"role": "avatar"}),
    )


def reaction_overlay(
    project: Project,
    profile: OutputProfile,
    *,
    asset: Asset,
    asset_ref: AssetRef,
    cell: NormalizedRect,
    duration_seconds: float,
    instance_key: str = "reaction",
    start_seconds: float = 0.0,
    z_index: int = 150,
    loop: bool = False,
) -> Overlay:
    _require_profile(project, profile)
    _visual(asset, label="reaction")
    if asset_ref.asset_id != asset.asset_id:
        raise ComponentRuntimeError("reaction AssetRef does not match the supplied asset")
    if duration_seconds <= 0.0:
        raise ComponentRuntimeError("reaction duration must be positive")
    if asset.media_type is MediaType.VIDEO:
        if loop:
            raise ComponentRuntimeError(
                "video reaction looping is not supported by the current FFmpeg component path"
            )
        if asset.duration_seconds is None:
            raise ComponentRuntimeError("reaction video duration metadata is required")
        if asset.duration_seconds + 1e-6 < duration_seconds:
            raise ComponentRuntimeError(
                "reaction video is shorter than the requested component duration"
            )
    return Overlay(
        overlay_id=_overlay_id(project, REACTION_COMPONENT_ID, instance_key),
        component_type=REACTION_COMPONENT_ID,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        placement=aspect_safe_rect(asset, profile, cell),
        z_index=z_index,
        asset_ref=asset_ref.validated_copy(update={"role": "reaction"}),
        properties={"loop": loop},
    )


def watermark_overlay(
    project: Project,
    profile: OutputProfile,
    *,
    placement: NormalizedRect,
    instance_key: str = "watermark",
    text: str | None = None,
    asset: Asset | None = None,
    asset_ref: AssetRef | None = None,
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
    z_index: int = 1000,
) -> Overlay:
    choices = int(text is not None) + int(asset is not None or asset_ref is not None)
    if choices != 1:
        raise ComponentRuntimeError("watermark requires exactly one of text or asset")
    if text is not None:
        return bounded_text_overlay(
            project,
            profile,
            component_type=WATERMARK_COMPONENT_ID,
            instance_key=instance_key,
            text=text,
            placement=placement,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            z_index=z_index,
            font_size_ratio=0.018,
            max_lines=2,
            box_color="black@0.35",
        )
    if asset is None or asset_ref is None:
        raise ComponentRuntimeError("asset watermark requires both Asset and AssetRef")
    _require_profile(project, profile)
    _visual(asset, label="watermark", image_only=True)
    if asset_ref.asset_id != asset.asset_id:
        raise ComponentRuntimeError("watermark AssetRef does not match the supplied asset")
    return Overlay(
        overlay_id=_overlay_id(project, WATERMARK_COMPONENT_ID, instance_key),
        component_type=WATERMARK_COMPONENT_ID,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        placement=aspect_safe_rect(asset, profile, placement),
        z_index=z_index,
        asset_ref=asset_ref.validated_copy(update={"role": "watermark"}),
    )


def _base_cover_rect(
    asset: Asset, profile: OutputProfile, placement: NormalizedRect
) -> tuple[float, float]:
    if asset.width is None or asset.height is None:
        raise ComponentRuntimeError("motion requires source asset dimensions")
    target_aspect = (placement.width * profile.width) / (placement.height * profile.height)
    source_aspect = asset.width / asset.height
    if source_aspect >= target_aspect:
        return target_aspect / source_aspect, 1.0
    return 1.0, source_aspect / target_aspect


def _focus_crop(
    width: float,
    height: float,
    *,
    zoom: float,
    focus: NormalizedPoint,
) -> NormalizedRect:
    if zoom < 1.0 or zoom > 8.0:
        raise ComponentRuntimeError("motion zoom must be between 1.0 and 8.0")
    width /= zoom
    height /= zoom
    x = min(max(focus.x - width / 2.0, 0.0), 1.0 - width)
    y = min(max(focus.y - height / 2.0, 0.0), 1.0 - height)
    return NormalizedRect(x=x, y=y, width=width, height=height)


def ken_burns_motion(
    asset: Asset,
    profile: OutputProfile,
    placement: NormalizedRect,
    *,
    focus: NormalizedPoint | None = None,
    start_zoom: float = 1.0,
    end_zoom: float = 1.08,
) -> MotionSpec:
    _visual(asset, label="ken_burns", image_only=True)
    focus = focus or NormalizedPoint(x=0.5, y=0.5)
    width, height = _base_cover_rect(asset, profile, placement)
    return MotionSpec(
        motion_type="slow_zoom",
        start_rect=_focus_crop(width, height, zoom=start_zoom, focus=focus),
        end_rect=_focus_crop(width, height, zoom=end_zoom, focus=focus),
        focus=focus,
        properties={"component_id": KEN_BURNS_COMPONENT_ID,
                    "component_version": PR13_COMPONENT_VERSION},
    )


def pan_motion(
    asset: Asset,
    profile: OutputProfile,
    placement: NormalizedRect,
    *,
    start_focus: NormalizedPoint,
    end_focus: NormalizedPoint,
    zoom: float = 1.12,
) -> MotionSpec:
    _visual(asset, label="pan", image_only=True)
    width, height = _base_cover_rect(asset, profile, placement)
    return MotionSpec(
        motion_type="pan",
        start_rect=_focus_crop(width, height, zoom=zoom, focus=start_focus),
        end_rect=_focus_crop(width, height, zoom=zoom, focus=end_focus),
        focus=end_focus,
        properties={"component_id": PAN_COMPONENT_ID,
                    "component_version": PR13_COMPONENT_VERSION},
    )


def crop_reveal_motion(
    asset: Asset,
    profile: OutputProfile,
    placement: NormalizedRect,
    *,
    focus: NormalizedPoint | None = None,
    start_zoom: float = 1.3,
    end_zoom: float = 1.0,
) -> MotionSpec:
    _visual(asset, label="crop_reveal", image_only=True)
    if start_zoom < end_zoom:
        raise ComponentRuntimeError("crop reveal start_zoom must be >= end_zoom")
    focus = focus or NormalizedPoint(x=0.5, y=0.5)
    width, height = _base_cover_rect(asset, profile, placement)
    return MotionSpec(
        motion_type="crop_reveal",
        start_rect=_focus_crop(width, height, zoom=start_zoom, focus=focus),
        end_rect=_focus_crop(width, height, zoom=end_zoom, focus=focus),
        focus=focus,
        properties={"component_id": CROP_REVEAL_COMPONENT_ID,
                    "component_version": PR13_COMPONENT_VERSION},
    )


def blur_reveal_motion(*, reveal_duration_seconds: float) -> MotionSpec:
    if reveal_duration_seconds <= 0.0:
        raise ComponentRuntimeError("blur reveal duration must be positive")
    return MotionSpec(
        motion_type="blur_reveal",
        properties={
            "component_id": BLUR_REVEAL_COMPONENT_ID,
            "component_version": PR13_COMPONENT_VERSION,
            "reveal_duration_seconds": reveal_duration_seconds,
        },
    )


def simple_transition(
    transition_type: str, *, duration_seconds: float = 0.15
) -> TransitionSpec:
    if transition_type not in _SIMPLE_TRANSITIONS:
        raise ComponentRuntimeError(f"unsupported simple transition type: {transition_type}")
    if duration_seconds <= 0.0 or duration_seconds > 5.0:
        raise ComponentRuntimeError("simple transition duration must be > 0 and <= 5 seconds")
    return TransitionSpec(
        transition_type=transition_type,
        duration_seconds=duration_seconds,
        properties={"component_id": TRANSITION_COMPONENT_ID,
                    "component_version": PR13_COMPONENT_VERSION},
    )


__all__ = [
    "ARTIST_CREDIT_COMPONENT", "ARTIST_CREDIT_COMPONENT_ID",
    "AVATAR_COMPONENT", "AVATAR_COMPONENT_ID",
    "BLUR_REVEAL_COMPONENT", "BLUR_REVEAL_COMPONENT_ID",
    "COMMENT_CARD_COMPONENT", "COMMENT_CARD_COMPONENT_ID",
    "CROP_REVEAL_COMPONENT", "CROP_REVEAL_COMPONENT_ID",
    "ComponentRuntimeError", "KEN_BURNS_COMPONENT", "KEN_BURNS_COMPONENT_ID",
    "PAN_COMPONENT", "PAN_COMPONENT_ID", "PR13_COMPONENTS", "PR13_COMPONENT_VERSION",
    "REACTION_COMPONENT", "REACTION_COMPONENT_ID",
    "TRANSITION_COMPONENT", "TRANSITION_COMPONENT_ID",
    "WATERMARK_COMPONENT", "WATERMARK_COMPONENT_ID",
    "artist_credit_overlay", "aspect_safe_rect", "avatar_overlay",
    "blur_reveal_motion", "bounded_text_overlay", "comment_card_overlay",
    "crop_reveal_motion", "ken_burns_motion", "pan_motion", "reaction_overlay",
    "simple_transition", "watermark_overlay",
]
