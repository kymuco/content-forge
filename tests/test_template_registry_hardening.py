from __future__ import annotations

import pytest

from content_forge.templates import (
    ComponentDefinition,
    ComponentRef,
    ComponentRegistry,
    RegistryReferenceError,
    SkinRegistry,
    TemplateAssetRegistry,
    TemplateDefinition,
    TemplateRegistration,
    TemplateRegistry,
    TemplateSlot,
)
from content_forge.timeline import ResolvedTemplate


def _resolver(project, assets, *, profile_id=None, variant_id=None):
    return ResolvedTemplate(
        template_id="bad_binding",
        version="1.0",
        properties={
            "resolved_profile_id": profile_id,
            "resolved_variant_id": variant_id,
        },
    )


def test_template_registration_rejects_text_slot_without_text_capability() -> None:
    assets = TemplateAssetRegistry()
    components = ComponentRegistry()
    components.register(
        ComponentDefinition(
            component_id="asset_only",
            version="1.0",
            output_kind="overlay",
            accepts_asset=True,
            description="Asset-only overlay component.",
        )
    )
    skins = SkinRegistry(assets)
    templates = TemplateRegistry(components, skins)
    definition = TemplateDefinition(
        template_id="bad_binding",
        version="1.0",
        description="Text slot must not bind an asset-only component.",
        components=(ComponentRef(component_id="asset_only", version="1.0"),),
        slots=(
            TemplateSlot(
                slot_id="title",
                slot_kind="text",
                component=ComponentRef(component_id="asset_only", version="1.0"),
            ),
        ),
    )

    with pytest.raises(RegistryReferenceError, match="without text capability"):
        templates.register(TemplateRegistration(definition=definition, resolver=_resolver))


def test_template_registration_rejects_asset_slot_without_asset_capability() -> None:
    assets = TemplateAssetRegistry()
    components = ComponentRegistry()
    components.register(
        ComponentDefinition(
            component_id="text_only",
            version="1.0",
            output_kind="overlay",
            accepts_text=True,
            description="Text-only overlay component.",
        )
    )
    skins = SkinRegistry(assets)
    templates = TemplateRegistry(components, skins)
    definition = TemplateDefinition(
        template_id="bad_binding",
        version="1.0",
        description="Media slot must not bind a text-only component.",
        components=(ComponentRef(component_id="text_only", version="1.0"),),
        slots=(
            TemplateSlot(
                slot_id="main",
                slot_kind="media",
                component=ComponentRef(component_id="text_only", version="1.0"),
            ),
        ),
    )

    with pytest.raises(RegistryReferenceError, match="without asset capability"):
        templates.register(TemplateRegistration(definition=definition, resolver=_resolver))


def test_component_contract_reserves_transition_output_kind() -> None:
    component = ComponentDefinition(
        component_id="crossfade",
        version="1.0",
        output_kind="transition",
        description="Schema reservation for future generic transition components.",
    )

    assert component.output_kind == "transition"
