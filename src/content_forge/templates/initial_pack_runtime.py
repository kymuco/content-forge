"""Runtime hardening wrappers for PR12 templates with profile-derived overlay geometry."""

from __future__ import annotations

from collections.abc import Mapping

from content_forge.core import Asset, AssetRef, FitMode, Project
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
    initial_template_definitions,
    resolve_art_story,
    resolve_content_frame,
    resolve_hook_topbar,
    resolve_meme_white_header,
    resolve_panel_sequence,
    resolve_reaction_bottom as _resolve_reaction_bottom,
    resolve_social_post,
    resolve_sync_stack as _resolve_sync_stack,
)
from .registry import TemplateRegistration

AssetSource = Mapping[str, Asset] | AssetResolver


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


def resolve_sync_stack(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _require_output_aspect_lock(project, SYNC_STACK_TEMPLATE_ID)
    resolved = _resolve_sync_stack(
        project,
        assets,
        profile_id=profile_id,
        variant_id=variant_id,
    )
    if resolved.scenes is None or len(resolved.scenes) != 1:
        raise InitialTemplateError("sync_stack resolver returned an invalid scene graph")
    scene = resolved.scenes[0].validated_copy(update={"fit_mode": FitMode.CONTAIN})
    return resolved.validated_copy(update={"scenes": (scene,)})


def resolve_reaction_bottom(
    project: Project,
    assets: AssetSource,
    *,
    profile_id: str | None = None,
    variant_id: str | None = None,
) -> ResolvedTemplate:
    _require_output_aspect_lock(project, REACTION_BOTTOM_TEMPLATE_ID)
    resolved = _resolve_reaction_bottom(
        project,
        assets,
        profile_id=profile_id,
        variant_id=variant_id,
    )
    if len(resolved.overlays) != 1:
        raise InitialTemplateError("reaction_bottom resolver returned an invalid overlay graph")
    reaction_id = resolved.properties.get("reaction_asset_id")
    if not isinstance(reaction_id, str):
        raise InitialTemplateError("reaction_bottom resolver omitted reaction asset identity")
    reaction = resolved.overlays[0].validated_copy(
        update={"asset_ref": _reaction_asset_ref(project, reaction_id)}
    )
    return resolved.validated_copy(update={"overlays": (reaction,)})


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
    "resolve_reaction_bottom",
    "resolve_sync_stack",
]
