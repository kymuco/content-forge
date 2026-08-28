"""Built-in versioned template/component registry definitions."""

from __future__ import annotations

from collections.abc import Mapping

from content_forge.core import Asset, NormalizedPoint, NormalizedRect, Project
from content_forge.timeline import AssetResolver, RenderPlan, ResolvedTemplate

from .contracts import (
    ComponentDefinition,
    ComponentRef,
    SkinDefinition,
    TemplateAnchor,
    TemplateAssetDefinition,
    TemplateDefault,
    TemplateDefinition,
    TemplateSafeZone,
    TemplateSlot,
)
from .hook_overlay import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    HookOverlayConfig,
    HookOverlayTemplateError,
    resolve_hook_overlay,
)
from .initial_pack import MEDIA_OVERLAY_COMPONENT
from .initial_pack_runtime import initial_template_registrations
from .registry import (
    ComponentRegistry,
    RegistryBundle,
    SkinRegistry,
    TemplateAssetRegistry,
    TemplateRegistration,
    TemplateRegistry,
)

BUILTIN_COMPONENT_VERSION = "1.0"
NEUTRAL_SKIN_ID = "neutral"
NEUTRAL_SKIN_VERSION = "1.0"
NEUTRAL_FRAME_ASSET_ID = "neutral_frame"
NEUTRAL_FRAME_ASSET_SHA256 = (
    "e2645d8ed07826fbdf520eb3fef16004dc2f15b4d32f2fa3662d759317cb224f"
)

MEDIA_COMPONENT = ComponentDefinition(
    component_id="media",
    version=BUILTIN_COMPONENT_VERSION,
    output_kind="scene",
    accepts_asset=True,
    description="Generic scene media placement resolved into ordinary Scene primitives.",
)

TEXT_COMPONENT = ComponentDefinition(
    component_id="text",
    version=BUILTIN_COMPONENT_VERSION,
    output_kind="overlay",
    accepts_text=True,
    required_properties=("font",),
    description="Generic text overlay contract consumed by renderer-independent timeline plans.",
)

ORIGINAL_AUDIO_COMPONENT = ComponentDefinition(
    component_id="original_audio",
    version=BUILTIN_COMPONENT_VERSION,
    output_kind="audio",
    accepts_asset=True,
    description="Original source-audio policy resolved into ordinary AudioTrack primitives.",
)

NEUTRAL_FRAME_ASSET = TemplateAssetDefinition(
    asset_id=NEUTRAL_FRAME_ASSET_ID,
    relative_path="assets/neutral-frame.svg",
    sha256=NEUTRAL_FRAME_ASSET_SHA256,
    license_spdx="Apache-2.0",
    redistributable=True,
    media_type="image_svg",
)

NEUTRAL_SKIN = SkinDefinition(
    skin_id=NEUTRAL_SKIN_ID,
    version=NEUTRAL_SKIN_VERSION,
    properties={
        "background": "transparent",
        "foreground": "white",
        "accent": "white",
    },
    asset_ids=(NEUTRAL_FRAME_ASSET_ID,),
    description="Redistribution-safe neutral skin fixture for registry and packaging tests.",
)


def hook_overlay_definition() -> TemplateDefinition:
    config = HookOverlayConfig()
    hook_rect = config.hook_region
    return TemplateDefinition(
        template_id=HOOK_OVERLAY_TEMPLATE_ID,
        version=HOOK_OVERLAY_TEMPLATE_VERSION,
        description=(
            "Full-canvas source media plus a bounded top hook overlay and optional "
            "original source audio."
        ),
        components=(
            ComponentRef(component_id="media", version=BUILTIN_COMPONENT_VERSION),
            ComponentRef(component_id="text", version=BUILTIN_COMPONENT_VERSION),
            ComponentRef(component_id="original_audio", version=BUILTIN_COMPONENT_VERSION),
        ),
        anchors=(
            TemplateAnchor(
                anchor_id="canvas_center",
                point=NormalizedPoint(x=0.5, y=0.5),
            ),
            TemplateAnchor(
                anchor_id="hook_origin",
                point=NormalizedPoint(x=hook_rect.x, y=hook_rect.y),
            ),
        ),
        safe_zones=(
            TemplateSafeZone(
                zone_id="hook_region",
                rect=hook_rect,
                policy="reserve",
                description="Region reserved for the hook text and its decoration budget.",
            ),
        ),
        slots=(
            TemplateSlot(
                slot_id="main_media",
                slot_kind="media",
                component=ComponentRef(
                    component_id="media",
                    version=BUILTIN_COMPONENT_VERSION,
                ),
                rect=NormalizedRect(x=0.0, y=0.0, width=1.0, height=1.0),
                anchor_id="canvas_center",
                description="Primary source media; one or more project scenes may fill this role.",
            ),
            TemplateSlot(
                slot_id="hook",
                slot_kind="text",
                component=ComponentRef(
                    component_id="text",
                    version=BUILTIN_COMPONENT_VERSION,
                ),
                rect=hook_rect,
                anchor_id="hook_origin",
                description="Human-accepted hook text from the selected Variant.",
            ),
        ),
        defaults=(
            TemplateDefault(key="source_fit", value=config.source_fit.value),
            TemplateDefault(key="font_size_ratio", value=config.font_size_ratio),
            TemplateDefault(key="border_width_ratio", value=config.border_width_ratio),
            TemplateDefault(key="max_glyph_width_em", value=config.max_glyph_width_em),
            TemplateDefault(key="line_height_em", value=config.line_height_em),
            TemplateDefault(key="max_lines", value=config.max_lines),
            TemplateDefault(key="font_color", value=config.font_color),
            TemplateDefault(key="border_color", value=config.border_color),
            TemplateDefault(key="box", value=config.box),
            TemplateDefault(key="box_color", value=config.box_color),
            TemplateDefault(key="original_audio", value=config.original_audio),
            TemplateDefault(
                key="original_audio_gain_db",
                value=config.original_audio_gain_db,
            ),
        ),
        metadata={
            "resolver": "content_forge.templates.hook_overlay:resolve_hook_overlay",
            "renderer_specific": False,
        },
    )


def create_builtin_registries() -> RegistryBundle:
    """Build fresh registries so callers cannot mutate a process-global singleton."""

    assets = TemplateAssetRegistry()
    assets.register(NEUTRAL_FRAME_ASSET)

    components = ComponentRegistry()
    components.register(MEDIA_COMPONENT)
    components.register(TEXT_COMPONENT)
    components.register(ORIGINAL_AUDIO_COMPONENT)
    components.register(MEDIA_OVERLAY_COMPONENT)

    skins = SkinRegistry(assets)
    skins.register(NEUTRAL_SKIN)

    templates = TemplateRegistry(components, skins)
    templates.register(
        TemplateRegistration(
            definition=hook_overlay_definition(),
            resolver=resolve_hook_overlay,
        )
    )
    for registration in initial_template_registrations():
        templates.register(registration)
    return RegistryBundle(
        assets=assets,
        components=components,
        skins=skins,
        templates=templates,
    )


def resolve_registered_template(
    project: Project,
    assets: Mapping[str, Asset] | AssetResolver,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    """Resolve the project's exact built-in registered template without compiling it."""

    return create_builtin_registries().templates.resolve(
        project,
        assets,
        profile_id=profile_id,
        variant_id=variant_id,
    )


def compile_registered_template(
    project: Project,
    assets: Mapping[str, Asset] | AssetResolver,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> RenderPlan:
    """Compile the project's exact registered template through the generic timeline path."""

    return create_builtin_registries().templates.compile(
        project,
        assets,
        profile_id=profile_id,
        variant_id=variant_id,
    )


def compile_registered_hook_overlay(
    project: Project,
    assets: Mapping[str, Asset] | AssetResolver,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> RenderPlan:
    """Registry-backed compatibility entry point for the built-in hook_overlay template."""

    if (
        project.template is None
        or project.template.template_id != HOOK_OVERLAY_TEMPLATE_ID
        or project.template.version != HOOK_OVERLAY_TEMPLATE_VERSION
    ):
        raise HookOverlayTemplateError(
            "project template must be "
            f"{HOOK_OVERLAY_TEMPLATE_ID}@{HOOK_OVERLAY_TEMPLATE_VERSION}"
        )
    return compile_registered_template(
        project,
        assets,
        profile_id=profile_id,
        variant_id=variant_id,
    )
