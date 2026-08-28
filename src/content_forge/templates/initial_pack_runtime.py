"""Runtime hardening wrappers for PR12 built-in templates."""

from __future__ import annotations

from collections.abc import Mapping

from content_forge.core import Asset, AssetRef, FitMode, MediaType, NormalizedRect, Project
from content_forge.timeline import AssetResolver, ResolvedTemplate

from .initial_pack import (
    ART_STORY_TEMPLATE_ID,
    CONTENT_FRAME_TEMPLATE_ID,
    HOOK_TOPBAR_TEMPLATE_ID,
    MEME_WHITE_HEADER_TEMPLATE_ID,
    PANEL_SEQUENCE_TEMPLATE_ID,
    REACTION_BOTTOM_TEMPLATE_ID,
    SOCIAL_POST_TEMPLATE_ID,
    SYNC_STACK_TEMPLATE_ID,
    InitialTemplateError,
    _hook_text,
    _reaction_asset_id,
    _resolve_header_layout,
    _select_variant,
    _validate_visual_asset,
    initial_template_definitions,
    resolve_art_story,
    resolve_content_frame as _resolve_content_frame,
    resolve_panel_sequence,
    resolve_reaction_bottom as _resolve_reaction_bottom,
    resolve_social_post as _resolve_social_post,
    resolve_sync_stack as _resolve_sync_stack,
)
from .registry import TemplateRegistration

AssetSource = Mapping[str, Asset] | AssetResolver


def _rebuild(
    resolved: ResolvedTemplate,
    *,
    scenes=None,
    overlays=None,
    extra_properties: Mapping[str, object] | None = None,
) -> ResolvedTemplate:
    """Rebuild from JSON serialization so nested frozen metadata stays valid JsonValue."""

    payload = resolved.model_dump(mode="json")
    if scenes is not None:
        payload["scenes"] = [scene.model_dump(mode="json") for scene in scenes]
    if overlays is not None:
        payload["overlays"] = [overlay.model_dump(mode="json") for overlay in overlays]
    if extra_properties:
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            raise InitialTemplateError("template resolver returned invalid JSON properties")
        properties.update(extra_properties)
    return ResolvedTemplate.model_validate(payload)


def _require_output_aspect_lock(project: Project, template_id: str) -> None:
    """Keep profile-derived normalized geometry identical across preview/final outputs."""

    if not project.output_profiles:
        raise InitialTemplateError(f"{template_id} requires at least one output profile")
    expected = project.output_profiles[0].width / project.output_profiles[0].height
    for profile in project.output_profiles[1:]:
        candidate = profile.width / profile.height
        if abs(candidate - expected) > 1e-9:
            raise InitialTemplateError(
                f"{template_id} requires all project output profiles to share one canvas aspect ratio"
            )


def _reaction_asset_ref(project: Project, asset_id: str) -> AssetRef:
    """Retain source lineage when one canonical project source unambiguously owns the asset."""

    candidates = [ref for ref in project.source_refs if ref.asset_id == asset_id]
    source_ids = {ref.source_id for ref in candidates if ref.source_id is not None}
    if len(source_ids) > 1:
        raise InitialTemplateError(
            "reaction_bottom reaction asset has ambiguous project source provenance"
        )
    if source_ids:
        return AssetRef(asset_id=asset_id, source_id=next(iter(source_ids)), role="reaction")
    return AssetRef(asset_id=asset_id, role="reaction")


def resolve_hook_topbar(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    variant = _select_variant(project, variant_id, required=True)
    text = _hook_text(variant, required=True)
    assert variant is not None and text is not None
    # 0.050 keeps the three-line conservative preview budget inside the declared 12%
    # region without weakening the shared PR6 text-safety checks.
    resolved = _resolve_header_layout(
        project,
        assets,
        template_id=HOOK_TOPBAR_TEMPLATE_ID,
        profile_id=profile_id,
        variant_id=variant.variant_id,
        text=text,
        header_region=NormalizedRect(x=0.06, y=0.06, width=0.80, height=0.12),
        media_region=NormalizedRect(x=0.0, y=0.22, width=1.0, height=0.78),
        fit_mode=FitMode.COVER,
        font_size_ratio=0.050,
        max_lines=3,
    )
    return _rebuild(resolved)


def resolve_social_post(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    return _rebuild(
        _resolve_social_post(
            project,
            assets,
            profile_id=profile_id,
            variant_id=variant_id,
        )
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
    return _rebuild(
        resolved,
        extra_properties={"white_header_mode": "bounded_drawtext_box_v1"},
    )


def resolve_content_frame(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    return _rebuild(
        _resolve_content_frame(
            project,
            assets,
            profile_id=profile_id,
            variant_id=variant_id,
        )
    )


def resolve_sync_stack(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _require_output_aspect_lock(project, SYNC_STACK_TEMPLATE_ID)
    if len(project.scenes) != 1 or project.scenes[0].media is None:
        raise InitialTemplateError("sync_stack requires exactly one source media scene")
    source_scene = project.scenes[0]
    source_asset = _validate_visual_asset(assets, source_scene.media.asset_id)
    if source_asset.media_type is MediaType.VIDEO:
        if source_scene.trim_start_seconds != 0.0 or source_scene.trim_duration_seconds is not None:
            raise InitialTemplateError(
                "sync_stack does not support trimmed video until asset overlays share the scene source timebase"
            )
        if source_asset.duration_seconds is None:
            raise InitialTemplateError("sync_stack video duration metadata is required")
        if source_asset.duration_seconds + 1e-6 < source_scene.duration_seconds:
            raise InitialTemplateError(
                "sync_stack source video is shorter than the synchronized scene duration"
            )

    resolved = _resolve_sync_stack(
        project,
        assets,
        profile_id=profile_id,
        variant_id=variant_id,
    )
    if resolved.scenes is None or len(resolved.scenes) != 1:
        raise InitialTemplateError("sync_stack resolver returned an invalid scene graph")
    scene = resolved.scenes[0].validated_copy(update={"fit_mode": FitMode.CONTAIN})
    overlays = tuple(
        overlay.validated_copy(update={"duration_seconds": source_scene.duration_seconds})
        if overlay.asset_ref is not None
        else overlay
        for overlay in resolved.overlays
    )
    return _rebuild(resolved, scenes=(scene,), overlays=overlays)


def resolve_reaction_bottom(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _require_output_aspect_lock(project, REACTION_BOTTOM_TEMPLATE_ID)
    if len(project.scenes) != 1 or project.scenes[0].media is None:
        raise InitialTemplateError("reaction_bottom requires exactly one primary media scene")
    source_scene = project.scenes[0]
    reaction_id = _reaction_asset_id(project)
    reaction_asset = _validate_visual_asset(assets, reaction_id)
    if reaction_asset.media_type is MediaType.VIDEO:
        if reaction_asset.duration_seconds is None:
            raise InitialTemplateError("reaction_bottom video duration metadata is required")
        if reaction_asset.duration_seconds + 1e-6 < source_scene.duration_seconds:
            raise InitialTemplateError(
                "reaction_bottom video is shorter than the primary scene duration"
            )

    resolved = _resolve_reaction_bottom(
        project,
        assets,
        profile_id=profile_id,
        variant_id=variant_id,
    )
    if len(resolved.overlays) != 1:
        raise InitialTemplateError("reaction_bottom resolver returned an invalid overlay graph")
    resolved_reaction_id = resolved.properties.get("reaction_asset_id")
    if not isinstance(resolved_reaction_id, str) or resolved_reaction_id != reaction_id:
        raise InitialTemplateError("reaction_bottom resolver returned mismatched reaction identity")
    reaction = resolved.overlays[0].validated_copy(
        update={
            "asset_ref": _reaction_asset_ref(project, reaction_id),
            "duration_seconds": source_scene.duration_seconds,
        }
    )
    return _rebuild(resolved, overlays=(reaction,))


def initial_template_registrations() -> tuple[TemplateRegistration, ...]:
    resolvers = {
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
    "initial_template_registrations",
    "resolve_content_frame",
    "resolve_hook_topbar",
    "resolve_meme_white_header",
    "resolve_reaction_bottom",
    "resolve_social_post",
    "resolve_sync_stack",
]
