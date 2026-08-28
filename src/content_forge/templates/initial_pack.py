"""PR12 initial non-voiced template pack over existing timeline primitives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Callable

from content_forge.core import (
    Asset,
    AssetRef,
    AudioTrack,
    EntityKind,
    FitMode,
    MediaType,
    NormalizedPoint,
    NormalizedRect,
    OutputProfile,
    Overlay,
    Project,
    Scene,
    TemplateRef,
    Variant,
    require_entity_id,
)
from content_forge.timeline import AssetResolver, ResolvedTemplate

from .contracts import (
    ComponentDefinition,
    ComponentRef,
    TemplateAnchor,
    TemplateDefault,
    TemplateDefinition,
    TemplateSafeZone,
    TemplateSlot,
)
from .hook_overlay import (
    HOOK_OVERLAY_FONT_FAMILY,
    HookOverlayConfig,
    HookOverlayTemplateError,
    _layout_metrics_for_profile,
    _wrap_hook,
)
from .registry import TemplateRegistration

INITIAL_TEMPLATE_VERSION = "1.0"
BUILTIN_COMPONENT_VERSION = "1.0"

HOOK_TOPBAR_TEMPLATE_ID = "hook_topbar"
SOCIAL_POST_TEMPLATE_ID = "social_post"
MEME_WHITE_HEADER_TEMPLATE_ID = "meme_white_header"
CONTENT_FRAME_TEMPLATE_ID = "content_frame"
ART_STORY_TEMPLATE_ID = "art_story"
PANEL_SEQUENCE_TEMPLATE_ID = "panel_sequence"
SYNC_STACK_TEMPLATE_ID = "sync_stack"
REACTION_BOTTOM_TEMPLATE_ID = "reaction_bottom"

INITIAL_TEMPLATE_IDS = (
    HOOK_TOPBAR_TEMPLATE_ID,
    SOCIAL_POST_TEMPLATE_ID,
    MEME_WHITE_HEADER_TEMPLATE_ID,
    CONTENT_FRAME_TEMPLATE_ID,
    ART_STORY_TEMPLATE_ID,
    PANEL_SEQUENCE_TEMPLATE_ID,
    SYNC_STACK_TEMPLATE_ID,
    REACTION_BOTTOM_TEMPLATE_ID,
)

MEDIA_OVERLAY_COMPONENT = ComponentDefinition(
    component_id="media_overlay",
    version=BUILTIN_COMPONENT_VERSION,
    output_kind="overlay",
    accepts_asset=True,
    description=(
        "Generic timed image/video overlay contract backed by the existing asset-overlay "
        "timeline primitive; semantic reactions remain PR13 scope."
    ),
)

_MEDIA = ComponentRef(component_id="media", version=BUILTIN_COMPONENT_VERSION)
_MEDIA_OVERLAY = ComponentRef(
    component_id=MEDIA_OVERLAY_COMPONENT.component_id,
    version=MEDIA_OVERLAY_COMPONENT.version,
)
_TEXT = ComponentRef(component_id="text", version=BUILTIN_COMPONENT_VERSION)
_ORIGINAL_AUDIO = ComponentRef(
    component_id="original_audio",
    version=BUILTIN_COMPONENT_VERSION,
)


class InitialTemplateError(ValueError):
    """Raised when one of the PR12 built-in templates cannot resolve safely."""


AssetSource = Mapping[str, Asset] | AssetResolver
Resolver = Callable[..., ResolvedTemplate]


def _derived_id(kind: EntityKind, *parts: str) -> str:
    payload = json.dumps(
        ["content-forge-derived-id-v1", kind.value, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(payload).hexdigest()[:32]
    return f"cf_{kind.value}_{suffix}"


def _asset_from(source: AssetSource, asset_id: str) -> Asset | None:
    if isinstance(source, Mapping):
        return source.get(asset_id)
    return source.get_asset(asset_id)


def _select_profile(project: Project, profile_id: str | None) -> OutputProfile:
    if profile_id is not None:
        for profile in project.output_profiles:
            if profile.profile_id == profile_id:
                return profile
        raise InitialTemplateError(f"unknown output profile: {profile_id}")
    if len(project.output_profiles) != 1:
        raise InitialTemplateError(
            "profile_id is required unless the project has exactly one output profile"
        )
    return project.output_profiles[0]


def _select_variant(
    project: Project,
    variant_id: str | None,
    *,
    required: bool,
) -> Variant | None:
    if variant_id is not None:
        for variant in project.variants:
            if variant.variant_id == variant_id:
                return variant
        raise InitialTemplateError(f"unknown variant: {variant_id}")
    if not project.variants:
        if required:
            raise InitialTemplateError("template requires a selected text variant")
        return None
    if len(project.variants) == 1:
        return project.variants[0]
    raise InitialTemplateError(
        "variant_id is required when the project has more than one variant"
    )


def _validate_template_ref(project: Project, template_id: str) -> None:
    expected = TemplateRef(template_id=template_id, version=INITIAL_TEMPLATE_VERSION)
    if project.template != expected:
        raise InitialTemplateError(
            f"project template must be {template_id}@{INITIAL_TEMPLATE_VERSION}"
        )


def _hook_text(variant: Variant | None, *, required: bool) -> str | None:
    if variant is None:
        if required:
            raise InitialTemplateError("template requires a selected text variant")
        return None
    value = variant.text_overrides.get("hook")
    if value is None:
        value = variant.hook
    if value is None or not value.strip():
        if required:
            raise InitialTemplateError("selected variant has no non-empty hook text")
        return None
    if "\x00" in value:
        raise InitialTemplateError("template text cannot contain NUL")
    return value.strip()


def _metadata_text(project: Project, key: str, *, max_length: int = 512) -> str | None:
    value = project.metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InitialTemplateError(f"project metadata {key!r} must be a string")
    value = value.strip()
    if not value:
        return None
    if len(value) > max_length:
        raise InitialTemplateError(f"project metadata {key!r} is too long")
    if "\x00" in value:
        raise InitialTemplateError(f"project metadata {key!r} cannot contain NUL")
    return value


def _validate_visual_asset(source: AssetSource, asset_id: str) -> Asset:
    asset = _asset_from(source, asset_id)
    if asset is None:
        raise InitialTemplateError(f"template cannot resolve asset: {asset_id}")
    if asset.asset_id != asset_id:
        raise InitialTemplateError("asset resolver returned mismatched asset identity")
    if asset.media_type not in {MediaType.IMAGE, MediaType.VIDEO}:
        raise InitialTemplateError(f"template asset must be image or video: {asset_id}")
    return asset


def _id_collides(project: Project, generated_id: str) -> bool:
    if any(item.overlay_id == generated_id for item in project.overlays):
        return True
    if any(item.audio_track_id == generated_id for item in project.audio_tracks):
        return True
    for scene in project.scenes:
        if any(item.overlay_id == generated_id for item in scene.overlays):
            return True
        if any(item.audio_track_id == generated_id for item in scene.audio_tracks):
            return True
    return False


def _with_original_audio(
    project: Project,
    scene: Scene,
    asset: Asset,
    *,
    template_id: str,
) -> tuple[AudioTrack, ...]:
    tracks = list(scene.audio_tracks)
    if asset.media_type is not MediaType.VIDEO:
        return tuple(tracks)
    if any(track.track_type == "original" for track in tracks):
        return tuple(tracks)
    if asset.has_audio is None:
        raise InitialTemplateError(f"video audio metadata is unknown: {asset.asset_id}")
    if not asset.has_audio:
        return tuple(tracks)
    audio_id = _derived_id(
        EntityKind.AUDIO,
        project.project_id,
        template_id,
        INITIAL_TEMPLATE_VERSION,
        scene.scene_id,
        "original-audio",
    )
    if _id_collides(project, audio_id):
        raise InitialTemplateError("generated original-audio ID collides with project state")
    tracks.append(AudioTrack(audio_track_id=audio_id, track_type="original"))
    return tuple(tracks)


def _scene_in_rect(
    project: Project,
    scene: Scene,
    assets: AssetSource,
    *,
    template_id: str,
    placement: NormalizedRect,
    fit_mode: FitMode,
    require_image: bool = False,
) -> Scene:
    if scene.media is None:
        raise InitialTemplateError(f"template scene has no media asset: {scene.scene_id}")
    asset = _validate_visual_asset(assets, scene.media.asset_id)
    if require_image and asset.media_type is not MediaType.IMAGE:
        raise InitialTemplateError(f"template requires image media: {asset.asset_id}")
    return scene.validated_copy(
        update={
            "placement": placement,
            "fit_mode": fit_mode,
            "audio_tracks": _with_original_audio(
                project,
                scene,
                asset,
                template_id=template_id,
            ),
        }
    )


def _text_overlay(
    project: Project,
    profile: OutputProfile,
    *,
    template_id: str,
    role: str,
    text: str,
    region: NormalizedRect,
    font_size_ratio: float,
    max_lines: int,
    font_color: str = "white",
    border_color: str = "black",
    box: bool = False,
    box_color: str = "black@0.55",
    border_width_ratio: float = 0.002,
    z_index: int = 100,
) -> tuple[Overlay, dict[str, object]]:
    config = HookOverlayConfig(
        hook_region=region,
        font_size_ratio=font_size_ratio,
        border_width_ratio=border_width_ratio,
        max_lines=max_lines,
        font_color=font_color,
        border_color=border_color,
        box=box,
        box_color=box_color,
    )
    try:
        wrapped, wrap_width, line_count = _wrap_hook(text, config)
        selected_metrics = None
        for candidate in project.output_profiles:
            metrics = _layout_metrics_for_profile(
                candidate,
                config,
                wrapped_hook=wrapped,
                line_count=line_count,
            )
            if candidate.profile_id == profile.profile_id:
                selected_metrics = metrics
    except HookOverlayTemplateError as exc:
        raise InitialTemplateError(f"{template_id} text layout is invalid: {exc}") from exc
    if selected_metrics is None:
        raise InitialTemplateError("selected output profile is missing from project outputs")

    font_size, border_width, region_width, _, required_width, region_height, _, required_height = (
        selected_metrics
    )
    overlay_id = _derived_id(
        EntityKind.OVERLAY,
        project.project_id,
        template_id,
        INITIAL_TEMPLATE_VERSION,
        role,
    )
    if _id_collides(project, overlay_id):
        raise InitialTemplateError(f"generated {role} overlay ID collides with project state")
    overlay = Overlay(
        overlay_id=overlay_id,
        component_type="text",
        placement=region,
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
        },
    )
    return overlay, {
        f"{role}_wrap_width_chars": wrap_width,
        f"{role}_line_count": line_count,
        f"{role}_font_size_pixels": font_size,
        f"{role}_region_width_pixels": region_width,
        f"{role}_required_width_pixels": required_width,
        f"{role}_region_height_pixels": region_height,
        f"{role}_required_height_pixels": required_height,
    }


def _base_properties(profile: OutputProfile, variant: Variant | None) -> dict[str, object]:
    return {
        "resolved_profile_id": profile.profile_id,
        "resolved_variant_id": None if variant is None else variant.variant_id,
    }


def _resolve_header_layout(
    project: Project,
    assets: AssetSource,
    *,
    template_id: str,
    profile_id: str | None,
    variant_id: str | None,
    text: str,
    header_region: NormalizedRect,
    media_region: NormalizedRect,
    fit_mode: FitMode,
    font_size_ratio: float,
    max_lines: int,
    font_color: str = "white",
    border_color: str = "black",
    box: bool = False,
    box_color: str = "black@0.55",
    border_width_ratio: float = 0.002,
) -> ResolvedTemplate:
    _validate_template_ref(project, template_id)
    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id, required=True)
    if not project.scenes:
        raise InitialTemplateError(f"{template_id} requires at least one source scene")
    scenes = tuple(
        _scene_in_rect(
            project,
            scene,
            assets,
            template_id=template_id,
            placement=media_region,
            fit_mode=fit_mode,
        )
        for scene in project.scenes
    )
    overlay, metrics = _text_overlay(
        project,
        profile,
        template_id=template_id,
        role="header",
        text=text,
        region=header_region,
        font_size_ratio=font_size_ratio,
        max_lines=max_lines,
        font_color=font_color,
        border_color=border_color,
        box=box,
        box_color=box_color,
        border_width_ratio=border_width_ratio,
    )
    properties = _base_properties(profile, variant)
    properties.update(metrics)
    properties["media_region"] = media_region.model_dump(mode="json")
    return ResolvedTemplate(
        template_id=template_id,
        version=INITIAL_TEMPLATE_VERSION,
        scenes=scenes,
        overlays=(overlay,),
        properties=properties,
    )


def resolve_hook_topbar(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    variant = _select_variant(project, variant_id, required=True)
    text = _hook_text(variant, required=True)
    assert text is not None
    return _resolve_header_layout(
        project,
        assets,
        template_id=HOOK_TOPBAR_TEMPLATE_ID,
        profile_id=profile_id,
        variant_id=variant.variant_id if variant else None,
        text=text,
        header_region=NormalizedRect(x=0.06, y=0.06, width=0.80, height=0.12),
        media_region=NormalizedRect(x=0.0, y=0.22, width=1.0, height=0.78),
        fit_mode=FitMode.COVER,
        font_size_ratio=0.052,
        max_lines=3,
    )


def resolve_social_post(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    variant = _select_variant(project, variant_id, required=True)
    hook = _hook_text(variant, required=True)
    assert variant is not None and hook is not None
    display_name = _metadata_text(project, "social_post.display_name") or variant.title or "Post"
    handle = _metadata_text(project, "social_post.handle", max_length=128)
    identity = display_name if handle is None else f"{display_name} {handle}"
    text = f"{identity}\n{hook}"
    return _resolve_header_layout(
        project,
        assets,
        template_id=SOCIAL_POST_TEMPLATE_ID,
        profile_id=profile_id,
        variant_id=variant.variant_id,
        text=text,
        header_region=NormalizedRect(x=0.06, y=0.06, width=0.80, height=0.18),
        media_region=NormalizedRect(x=0.0, y=0.28, width=1.0, height=0.72),
        fit_mode=FitMode.COVER,
        font_size_ratio=0.038,
        max_lines=5,
        box=True,
        box_color="black@0.72",
    )


def resolve_meme_white_header(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    variant = _select_variant(project, variant_id, required=True)
    text = _hook_text(variant, required=True)
    assert variant is not None and text is not None
    resolved = _resolve_header_layout(
        project,
        assets,
        template_id=MEME_WHITE_HEADER_TEMPLATE_ID,
        profile_id=profile_id,
        variant_id=variant.variant_id,
        text=text,
        header_region=NormalizedRect(x=0.04, y=0.06, width=0.82, height=0.18),
        media_region=NormalizedRect(x=0.0, y=0.28, width=1.0, height=0.72),
        fit_mode=FitMode.COVER,
        font_size_ratio=0.045,
        max_lines=4,
        font_color="black",
        border_color="white",
        box=True,
        box_color="white",
        border_width_ratio=0.0,
    )
    properties = dict(resolved.properties)
    properties["white_header_mode"] = "bounded_drawtext_box_v1"
    return resolved.validated_copy(update={"properties": properties})


def resolve_content_frame(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _validate_template_ref(project, CONTENT_FRAME_TEMPLATE_ID)
    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id, required=False)
    if not project.scenes:
        raise InitialTemplateError("content_frame requires at least one source scene")
    media_region = NormalizedRect(x=0.06, y=0.18, width=0.78, height=0.64)
    scenes = tuple(
        _scene_in_rect(
            project,
            scene,
            assets,
            template_id=CONTENT_FRAME_TEMPLATE_ID,
            placement=media_region,
            fit_mode=FitMode.CONTAIN,
        )
        for scene in project.scenes
    )
    overlays: tuple[Overlay, ...] = ()
    properties = _base_properties(profile, variant)
    text = _hook_text(variant, required=False)
    if text is not None:
        header, metrics = _text_overlay(
            project,
            profile,
            template_id=CONTENT_FRAME_TEMPLATE_ID,
            role="header",
            text=text,
            region=NormalizedRect(x=0.06, y=0.06, width=0.78, height=0.08),
            font_size_ratio=0.034,
            max_lines=2,
        )
        overlays = (header,)
        properties.update(metrics)
    properties["media_region"] = media_region.model_dump(mode="json")
    return ResolvedTemplate(
        template_id=CONTENT_FRAME_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        scenes=scenes,
        overlays=overlays,
        properties=properties,
    )


def _credit_text(project: Project) -> str | None:
    credits: list[str] = []
    for record in project.source_records:
        if record.credit_text and record.credit_text.strip():
            value = record.credit_text.strip()
            if value not in credits:
                credits.append(value)
    if not credits:
        return None
    text = " / ".join(credits)
    if len(text) > 512:
        raise InitialTemplateError("combined art-story credit text is too long")
    return text


def resolve_art_story(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _validate_template_ref(project, ART_STORY_TEMPLATE_ID)
    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id, required=False)
    if not 1 <= len(project.scenes) <= 32:
        raise InitialTemplateError("art_story requires between 1 and 32 scenes")
    media_region = NormalizedRect(x=0.04, y=0.05, width=0.82, height=0.76)
    scenes = tuple(
        _scene_in_rect(
            project,
            scene,
            assets,
            template_id=ART_STORY_TEMPLATE_ID,
            placement=media_region,
            fit_mode=FitMode.CONTAIN,
            require_image=True,
        )
        for scene in project.scenes
    )
    overlays: tuple[Overlay, ...] = ()
    properties = _base_properties(profile, variant)
    credit = _credit_text(project)
    if credit is not None:
        credit_overlay, metrics = _text_overlay(
            project,
            profile,
            template_id=ART_STORY_TEMPLATE_ID,
            role="credit",
            text=credit,
            region=NormalizedRect(x=0.05, y=0.82, width=0.80, height=0.05),
            font_size_ratio=0.022,
            max_lines=2,
            box=True,
            box_color="black@0.65",
            border_width_ratio=0.001,
        )
        overlays = (credit_overlay,)
        properties.update(metrics)
    properties["sequence_length"] = len(scenes)
    return ResolvedTemplate(
        template_id=ART_STORY_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        scenes=scenes,
        overlays=overlays,
        properties=properties,
    )


def resolve_panel_sequence(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _validate_template_ref(project, PANEL_SEQUENCE_TEMPLATE_ID)
    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id, required=False)
    if not 1 <= len(project.scenes) <= 64:
        raise InitialTemplateError("panel_sequence requires between 1 and 64 scenes")
    media_region = NormalizedRect(x=0.02, y=0.05, width=0.84, height=0.80)
    scenes = tuple(
        _scene_in_rect(
            project,
            scene,
            assets,
            template_id=PANEL_SEQUENCE_TEMPLATE_ID,
            placement=media_region,
            fit_mode=FitMode.CONTAIN,
            require_image=True,
        )
        for scene in project.scenes
    )
    properties = _base_properties(profile, variant)
    properties.update({"sequence_length": len(scenes), "pacing": "project_scene_durations"})
    return ResolvedTemplate(
        template_id=PANEL_SEQUENCE_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        scenes=scenes,
        properties=properties,
    )


def _aspect_rect(
    asset: Asset,
    profile: OutputProfile,
    cell: NormalizedRect,
) -> NormalizedRect:
    if asset.width is None or asset.height is None:
        raise InitialTemplateError(
            f"asset dimensions are required for aspect-safe overlay placement: {asset.asset_id}"
        )
    cell_width_pixels = cell.width * profile.width
    cell_height_pixels = cell.height * profile.height
    scale = min(cell_width_pixels / asset.width, cell_height_pixels / asset.height)
    width = asset.width * scale / profile.width
    height = asset.height * scale / profile.height
    return NormalizedRect(
        x=cell.x + (cell.width - width) / 2.0,
        y=cell.y + (cell.height - height) / 2.0,
        width=width,
        height=height,
    )


def _sync_copies(project: Project) -> int:
    value = project.metadata.get("sync_stack.copies", 2)
    if isinstance(value, bool) or not isinstance(value, int) or value not in {2, 3}:
        raise InitialTemplateError("sync_stack.copies must be integer 2 or 3")
    return value


def resolve_sync_stack(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _validate_template_ref(project, SYNC_STACK_TEMPLATE_ID)
    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id, required=False)
    if len(project.scenes) != 1 or project.scenes[0].media is None:
        raise InitialTemplateError("sync_stack requires exactly one source media scene")
    source_scene = project.scenes[0]
    asset = _validate_visual_asset(assets, source_scene.media.asset_id)
    copies = _sync_copies(project)
    gap = 0.02
    top = 0.18
    bottom = 0.86
    cell_height = (bottom - top - gap * (copies - 1)) / copies
    cells = tuple(
        NormalizedRect(
            x=0.05,
            y=top + index * (cell_height + gap),
            width=0.78,
            height=cell_height,
        )
        for index in range(copies)
    )
    rects = tuple(_aspect_rect(asset, profile, cell) for cell in cells)
    first = _scene_in_rect(
        project,
        source_scene,
        assets,
        template_id=SYNC_STACK_TEMPLATE_ID,
        placement=rects[0],
        fit_mode=FitMode.STRETCH,
    )
    overlays: list[Overlay] = []
    for index, rect in enumerate(rects[1:], start=1):
        overlay_id = _derived_id(
            EntityKind.OVERLAY,
            project.project_id,
            SYNC_STACK_TEMPLATE_ID,
            INITIAL_TEMPLATE_VERSION,
            f"copy-{index}",
        )
        if _id_collides(project, overlay_id):
            raise InitialTemplateError("generated sync copy overlay ID collides with project state")
        overlays.append(
            Overlay(
                overlay_id=overlay_id,
                component_type="media_overlay",
                placement=rect,
                z_index=20 + index,
                asset_ref=source_scene.media,
            )
        )
    properties = _base_properties(profile, variant)
    headline = _hook_text(variant, required=False)
    if headline is not None:
        header, metrics = _text_overlay(
            project,
            profile,
            template_id=SYNC_STACK_TEMPLATE_ID,
            role="header",
            text=headline,
            region=NormalizedRect(x=0.05, y=0.06, width=0.78, height=0.08),
            font_size_ratio=0.033,
            max_lines=2,
        )
        overlays.append(header)
        properties.update(metrics)
    properties.update(
        {
            "copies": copies,
            "source_asset_id": asset.asset_id,
            "aspect_safe_rects": [rect.model_dump(mode="json") for rect in rects],
        }
    )
    return ResolvedTemplate(
        template_id=SYNC_STACK_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        scenes=(first,),
        overlays=tuple(overlays),
        properties=properties,
    )


def _reaction_asset_id(project: Project) -> str:
    value = project.metadata.get("reaction_bottom.reaction_asset_id")
    if not isinstance(value, str):
        raise InitialTemplateError(
            "reaction_bottom.reaction_asset_id must name a Content Forge asset"
        )
    try:
        return require_entity_id(value, EntityKind.ASSET)
    except ValueError as exc:
        raise InitialTemplateError(
            "reaction_bottom.reaction_asset_id must be a Content Forge asset ID"
        ) from exc


def resolve_reaction_bottom(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _validate_template_ref(project, REACTION_BOTTOM_TEMPLATE_ID)
    profile = _select_profile(project, profile_id)
    variant = _select_variant(project, variant_id, required=False)
    if len(project.scenes) != 1 or project.scenes[0].media is None:
        raise InitialTemplateError("reaction_bottom requires exactly one primary media scene")
    source_scene = project.scenes[0]
    _validate_visual_asset(assets, source_scene.media.asset_id)
    reaction_id = _reaction_asset_id(project)
    if reaction_id == source_scene.media.asset_id:
        raise InitialTemplateError("reaction asset must be distinct from primary media")
    reaction_asset = _validate_visual_asset(assets, reaction_id)
    main_region = NormalizedRect(x=0.0, y=0.05, width=0.88, height=0.56)
    reaction_cell = NormalizedRect(x=0.06, y=0.65, width=0.76, height=0.20)
    reaction_rect = _aspect_rect(reaction_asset, profile, reaction_cell)
    scene = _scene_in_rect(
        project,
        source_scene,
        assets,
        template_id=REACTION_BOTTOM_TEMPLATE_ID,
        placement=main_region,
        fit_mode=FitMode.COVER,
    )
    overlay_id = _derived_id(
        EntityKind.OVERLAY,
        project.project_id,
        REACTION_BOTTOM_TEMPLATE_ID,
        INITIAL_TEMPLATE_VERSION,
        "reaction",
    )
    if _id_collides(project, overlay_id):
        raise InitialTemplateError("generated reaction overlay ID collides with project state")
    reaction = Overlay(
        overlay_id=overlay_id,
        component_type="media_overlay",
        placement=reaction_rect,
        z_index=50,
        asset_ref=AssetRef(asset_id=reaction_id, role="reaction"),
    )
    properties = _base_properties(profile, variant)
    properties.update(
        {
            "primary_asset_id": source_scene.media.asset_id,
            "reaction_asset_id": reaction_id,
            "reaction_rect": reaction_rect.model_dump(mode="json"),
        }
    )
    return ResolvedTemplate(
        template_id=REACTION_BOTTOM_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        scenes=(scene,),
        overlays=(reaction,),
        properties=properties,
    )


def _header_definition(
    template_id: str,
    *,
    description: str,
    header_region: NormalizedRect,
    media_region: NormalizedRect,
) -> TemplateDefinition:
    return TemplateDefinition(
        template_id=template_id,
        version=INITIAL_TEMPLATE_VERSION,
        description=description,
        components=(_MEDIA, _TEXT, _ORIGINAL_AUDIO),
        anchors=(
            TemplateAnchor(anchor_id="header_origin", point=NormalizedPoint(x=header_region.x, y=header_region.y)),
            TemplateAnchor(anchor_id="media_center", point=NormalizedPoint(x=0.5, y=media_region.y + media_region.height / 2.0)),
        ),
        safe_zones=(
            TemplateSafeZone(
                zone_id="header_region",
                rect=header_region,
                policy="reserve",
                description="Reserved presentation text region outside platform UI safe zones.",
            ),
        ),
        slots=(
            TemplateSlot(
                slot_id="main_media",
                slot_kind="media",
                component=_MEDIA,
                rect=media_region,
                anchor_id="media_center",
            ),
            TemplateSlot(
                slot_id="header_text",
                slot_kind="text",
                component=_TEXT,
                rect=header_region,
                anchor_id="header_origin",
            ),
        ),
        metadata={"renderer_specific": False, "pack": "initial_v1"},
    )


def initial_template_definitions() -> tuple[TemplateDefinition, ...]:
    hook_topbar = _header_definition(
        HOOK_TOPBAR_TEMPLATE_ID,
        description="Dedicated top hook region with source media below.",
        header_region=NormalizedRect(x=0.06, y=0.06, width=0.80, height=0.12),
        media_region=NormalizedRect(x=0.0, y=0.22, width=1.0, height=0.78),
    )
    social_post = _header_definition(
        SOCIAL_POST_TEMPLATE_ID,
        description="Post-like identity/caption header with source media below.",
        header_region=NormalizedRect(x=0.06, y=0.06, width=0.80, height=0.18),
        media_region=NormalizedRect(x=0.0, y=0.28, width=1.0, height=0.72),
    )
    meme_white_header = _header_definition(
        MEME_WHITE_HEADER_TEMPLATE_ID,
        description="Bounded white meme text header with source media below.",
        header_region=NormalizedRect(x=0.04, y=0.06, width=0.82, height=0.18),
        media_region=NormalizedRect(x=0.0, y=0.28, width=1.0, height=0.72),
    )
    content_frame = TemplateDefinition(
        template_id=CONTENT_FRAME_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        description="Generic framed media layout suitable for anime/game/clip branding skins.",
        components=(_MEDIA, _TEXT, _ORIGINAL_AUDIO),
        anchors=(TemplateAnchor(anchor_id="media_center", point=NormalizedPoint(x=0.45, y=0.5)),),
        slots=(
            TemplateSlot(
                slot_id="main_media",
                slot_kind="media",
                component=_MEDIA,
                rect=NormalizedRect(x=0.06, y=0.18, width=0.78, height=0.64),
                anchor_id="media_center",
            ),
            TemplateSlot(slot_id="header_text", slot_kind="text", component=_TEXT, required=False),
        ),
        defaults=(TemplateDefault(key="source_fit", value="contain"),),
        metadata={"renderer_specific": False, "pack": "initial_v1", "anime_frame_use_case": True},
    )
    art_story = TemplateDefinition(
        template_id=ART_STORY_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        description="Ordered image story with readable contain geometry and optional source credit.",
        components=(_MEDIA, _TEXT),
        slots=(
            TemplateSlot(
                slot_id="art_sequence",
                slot_kind="component",
                component=_MEDIA,
                description="One to 32 ordered canonical image scenes.",
            ),
            TemplateSlot(slot_id="credit", slot_kind="text", component=_TEXT, required=False),
        ),
        defaults=(TemplateDefault(key="max_scenes", value=32), TemplateDefault(key="source_fit", value="contain")),
        metadata={"renderer_specific": False, "pack": "initial_v1"},
    )
    panel_sequence = TemplateDefinition(
        template_id=PANEL_SEQUENCE_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        description="Readable ordered comic/manga/manhwa image sequence using project timing.",
        components=(_MEDIA,),
        slots=(
            TemplateSlot(
                slot_id="panels",
                slot_kind="component",
                component=_MEDIA,
                description="One to 64 ordered canonical panel scenes.",
            ),
        ),
        defaults=(TemplateDefault(key="max_scenes", value=64), TemplateDefault(key="source_fit", value="contain")),
        metadata={"renderer_specific": False, "pack": "initial_v1"},
    )
    sync_stack = TemplateDefinition(
        template_id=SYNC_STACK_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        description="Two or three synchronized aspect-safe copies of one source with optional premise text.",
        components=(_MEDIA, _MEDIA_OVERLAY, _TEXT, _ORIGINAL_AUDIO),
        slots=(
            TemplateSlot(slot_id="source", slot_kind="media", component=_MEDIA),
            TemplateSlot(slot_id="copies", slot_kind="component", component=_MEDIA_OVERLAY),
            TemplateSlot(slot_id="headline", slot_kind="text", component=_TEXT, required=False),
        ),
        defaults=(TemplateDefault(key="copies", value=2),),
        metadata={"renderer_specific": False, "pack": "initial_v1", "copies_allowed": [2, 3]},
    )
    reaction_bottom = TemplateDefinition(
        template_id=REACTION_BOTTOM_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
        description="Primary media with a distinct aspect-safe reaction asset in the lower composition region.",
        components=(_MEDIA, _MEDIA_OVERLAY, _ORIGINAL_AUDIO),
        slots=(
            TemplateSlot(slot_id="main_media", slot_kind="media", component=_MEDIA),
            TemplateSlot(slot_id="reaction_media", slot_kind="media", component=_MEDIA_OVERLAY),
        ),
        metadata={"renderer_specific": False, "pack": "initial_v1"},
    )
    return (
        hook_topbar,
        social_post,
        meme_white_header,
        content_frame,
        art_story,
        panel_sequence,
        sync_stack,
        reaction_bottom,
    )


def initial_template_registrations() -> tuple[TemplateRegistration, ...]:
    resolvers: dict[str, Resolver] = {
        HOOK_TOPBAR_TEMPLATE_ID: resolve_hook_topbar,
        SOCIAL_POST_TEMPLATE_ID: resolve_social_post,
        MEME_WHITE_HEADER_TEMPLATE_ID: resolve_meme_white_header,
        CONTENT_FRAME_TEMPLATE_ID: resolve_content_frame,
        ART_STORY_TEMPLATE_ID: resolve_art_story,
        PANEL_SEQUENCE_TEMPLATE_ID: resolve_panel_sequence,
        SYNC_STACK_TEMPLATE_ID: resolve_sync_stack,
        REACTION_BOTTOM_TEMPLATE_ID: resolve_reaction_bottom,
    }
    return tuple(
        TemplateRegistration(definition=definition, resolver=resolvers[definition.template_id])
        for definition in initial_template_definitions()
    )


__all__ = [
    "ART_STORY_TEMPLATE_ID",
    "CONTENT_FRAME_TEMPLATE_ID",
    "HOOK_TOPBAR_TEMPLATE_ID",
    "INITIAL_TEMPLATE_IDS",
    "INITIAL_TEMPLATE_VERSION",
    "InitialTemplateError",
    "MEDIA_OVERLAY_COMPONENT",
    "MEME_WHITE_HEADER_TEMPLATE_ID",
    "PANEL_SEQUENCE_TEMPLATE_ID",
    "REACTION_BOTTOM_TEMPLATE_ID",
    "SOCIAL_POST_TEMPLATE_ID",
    "SYNC_STACK_TEMPLATE_ID",
    "initial_template_definitions",
    "initial_template_registrations",
    "resolve_art_story",
    "resolve_content_frame",
    "resolve_hook_topbar",
    "resolve_meme_white_header",
    "resolve_panel_sequence",
    "resolve_reaction_bottom",
    "resolve_social_post",
    "resolve_sync_stack",
]
