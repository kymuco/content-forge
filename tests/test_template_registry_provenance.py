from __future__ import annotations

import json

import pytest

from content_forge.core import Project, Scene, TemplateRef
from content_forge.profiles import shorts_preview_profile
from content_forge.templates import (
    REGISTRY_EVIDENCE_PROPERTY,
    ComponentDefinition,
    ComponentRef,
    ComponentRegistry,
    SkinDefinition,
    SkinRef,
    SkinRegistry,
    TemplateAssetDefinition,
    TemplateAssetRegistry,
    TemplateDefinition,
    TemplateRegistration,
    TemplateRegistry,
    TemplateResolutionRegistryError,
)
from content_forge.timeline import ResolvedTemplate


def _project() -> Project:
    return Project(
        content_kind="test",
        template=TemplateRef(template_id="skin_template", version="1.0"),
        scenes=(Scene(order=0, duration_seconds=1.0),),
        output_profiles=(shorts_preview_profile(),),
    )


def _registry(resolver) -> TemplateRegistry:
    assets = TemplateAssetRegistry()
    assets.register(
        TemplateAssetDefinition(
            asset_id="frame",
            relative_path="assets/frame.svg",
            sha256="d" * 64,
            license_spdx="Apache-2.0",
            redistributable=True,
            media_type="image_svg",
        )
    )
    components = ComponentRegistry()
    components.register(
        ComponentDefinition(
            component_id="noop",
            version="3.2",
            output_kind="overlay",
            description="Provenance fixture component.",
        )
    )
    skins = SkinRegistry(assets)
    skins.register(
        SkinDefinition(
            skin_id="fixture_skin",
            version="4.1",
            asset_ids=("frame",),
            description="Provenance fixture skin.",
        )
    )
    templates = TemplateRegistry(components, skins)
    templates.register(
        TemplateRegistration(
            definition=TemplateDefinition(
                template_id="skin_template",
                version="1.0",
                description="Template with versioned component and skin dependencies.",
                components=(ComponentRef(component_id="noop", version="3.2"),),
                skins=(SkinRef(skin_id="fixture_skin", version="4.1"),),
                slots=(),
            ),
            resolver=resolver,
        )
    )
    return templates


def test_registry_evidence_records_component_skin_and_packaged_asset_identity() -> None:
    def resolver(project, assets, *, profile_id=None, variant_id=None):
        return ResolvedTemplate(
            template_id="skin_template",
            version="1.0",
            properties={
                "resolved_profile_id": profile_id,
                "resolved_variant_id": variant_id,
            },
        )

    resolved = _registry(resolver).resolve(_project(), {})
    evidence_json = resolved.properties[REGISTRY_EVIDENCE_PROPERTY]
    assert isinstance(evidence_json, str)
    evidence = json.loads(evidence_json)

    assert evidence["template"]["template_id"] == "skin_template"
    assert evidence["template"]["version"] == "1.0"
    assert len(evidence["template"]["definition_sha256"]) == 64
    assert evidence["components"][0]["component_id"] == "noop"
    assert evidence["components"][0]["version"] == "3.2"
    assert len(evidence["components"][0]["definition_sha256"]) == 64
    assert evidence["skins"][0]["skin_id"] == "fixture_skin"
    assert evidence["skins"][0]["version"] == "4.1"
    assert len(evidence["skins"][0]["definition_sha256"]) == 64
    assert evidence["skins"][0]["packaged_assets"] == [
        {
            "asset_id": "frame",
            "sha256": "d" * 64,
            "license_spdx": "Apache-2.0",
        }
    ]


def test_registry_preserves_nested_json_properties_while_injecting_evidence() -> None:
    nested = {
        "rect": {"x": 0.1, "y": 0.2, "width": 0.7, "height": 0.5},
        "items": [{"order": 0}, {"order": 1}],
    }

    def resolver(project, assets, *, profile_id=None, variant_id=None):
        return ResolvedTemplate(
            template_id="skin_template",
            version="1.0",
            properties={
                "resolved_profile_id": profile_id,
                "resolved_variant_id": variant_id,
                "layout": nested,
            },
        )

    resolved = _registry(resolver).resolve(_project(), {})

    assert resolved.model_dump(mode="json")["properties"]["layout"] == nested
    assert isinstance(resolved.properties[REGISTRY_EVIDENCE_PROPERTY], str)


def test_resolver_cannot_spoof_reserved_registry_evidence() -> None:
    def resolver(project, assets, *, profile_id=None, variant_id=None):
        return ResolvedTemplate(
            template_id="skin_template",
            version="1.0",
            properties={
                "resolved_profile_id": profile_id,
                "resolved_variant_id": variant_id,
                REGISTRY_EVIDENCE_PROPERTY: "{\"spoofed\":true}",
            },
        )

    with pytest.raises(
        TemplateResolutionRegistryError,
        match="reserved registry evidence",
    ):
        _registry(resolver).resolve(_project(), {})
