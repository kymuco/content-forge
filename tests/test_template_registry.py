from __future__ import annotations

import hashlib
import json
from importlib.resources import files

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    NormalizedPoint,
    NormalizedRect,
    Project,
    Scene,
    TemplateRef,
    Variant,
    new_entity_id,
)
from content_forge.profiles import (
    SHORTS_PREVIEW_PROFILE_ID,
    shorts_final_profile,
    shorts_preview_profile,
)
from content_forge.templates import (
    BUILTIN_COMPONENT_VERSION,
    COMPONENT_ENTRY_POINT_GROUP,
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    MEDIA_COMPONENT,
    NEUTRAL_FRAME_ASSET,
    NEUTRAL_FRAME_ASSET_SHA256,
    NEUTRAL_SKIN,
    REGISTRY_EVIDENCE_PROPERTY,
    TEMPLATE_ENTRY_POINT_GROUP,
    ComponentDefinition,
    ComponentRef,
    ComponentRegistry,
    DuplicateRegistryEntryError,
    RegistryReferenceError,
    SkinDefinition,
    SkinRegistry,
    TemplateAnchor,
    TemplateAssetDefinition,
    TemplateAssetRegistry,
    TemplateContractError,
    TemplateDefinition,
    TemplateRegistration,
    TemplateRegistry,
    TemplateResolutionRegistryError,
    TemplateSlot,
    UnknownRegistryEntryError,
    compile_hook_overlay,
    compile_registered_template,
    create_builtin_registries,
    discover_plugin_candidates,
    hook_overlay_definition,
)
from content_forge.templates.hook_overlay import (
    compile_hook_overlay as compile_hook_overlay_legacy,
)
from content_forge.timeline import ResolvedTemplate, render_plan_digest


def _image_asset() -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256="a" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=123,
        width=720,
        height=1280,
        has_audio=False,
    )


def _hook_project(asset: Asset, *, version: str = HOOK_OVERLAY_TEMPLATE_VERSION) -> Project:
    variant = Variant(language="en", hook="Registry path stays renderer independent")
    return Project(
        content_kind="character_moment",
        template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=version,
        ),
        variants=(variant,),
        scenes=(
            Scene(
                order=0,
                duration_seconds=2.0,
                media=AssetRef(asset_id=asset.asset_id),
            ),
        ),
        output_profiles=(shorts_preview_profile(), shorts_final_profile()),
    )


def test_builtin_hook_overlay_has_declarative_versioned_contract() -> None:
    definition = hook_overlay_definition()

    assert definition.template_id == HOOK_OVERLAY_TEMPLATE_ID
    assert definition.version == HOOK_OVERLAY_TEMPLATE_VERSION
    assert {slot.slot_id for slot in definition.slots} == {"main_media", "hook"}
    assert {anchor.anchor_id for anchor in definition.anchors} == {
        "canvas_center",
        "hook_origin",
    }
    assert {zone.zone_id for zone in definition.safe_zones} == {"hook_region"}
    assert {
        (component.component_id, component.version)
        for component in definition.components
    } == {
        ("media", BUILTIN_COMPONENT_VERSION),
        ("text", BUILTIN_COMPONENT_VERSION),
        ("original_audio", BUILTIN_COMPONENT_VERSION),
    }
    defaults = {item.key: item.value for item in definition.defaults}
    assert defaults["source_fit"] == "cover"
    assert defaults["max_lines"] == 4
    assert definition.metadata["renderer_specific"] is False


def test_template_definition_rejects_dangling_anchor_and_component_refs() -> None:
    with pytest.raises(ValueError, match="undeclared component"):
        TemplateDefinition(
            template_id="bad_template",
            version="1.0",
            description="Bad component reference.",
            components=(),
            slots=(
                TemplateSlot(
                    slot_id="main",
                    slot_kind="media",
                    component=ComponentRef(component_id="missing", version="1.0"),
                ),
            ),
        )

    with pytest.raises(ValueError, match="unknown anchor"):
        TemplateDefinition(
            template_id="bad_anchor",
            version="1.0",
            description="Bad anchor reference.",
            components=(ComponentRef(component_id="media", version="1.0"),),
            slots=(
                TemplateSlot(
                    slot_id="main",
                    slot_kind="media",
                    component=ComponentRef(component_id="media", version="1.0"),
                    anchor_id="missing",
                ),
            ),
        )


def test_slot_binding_validation_is_bounded_and_asset_identity_safe() -> None:
    definition = hook_overlay_definition()
    asset_id = new_entity_id(EntityKind.ASSET)

    assert definition.validate_slot_bindings(
        {"main_media": asset_id, "hook": "A bounded hook"}
    ) == {"main_media": asset_id, "hook": "A bounded hook"}

    with pytest.raises(TemplateContractError, match="missing required"):
        definition.validate_slot_bindings({"main_media": asset_id})
    with pytest.raises(TemplateContractError, match="unknown template slot"):
        definition.validate_slot_bindings(
            {"main_media": asset_id, "hook": "ok", "extra": "no"}
        )
    with pytest.raises(TemplateContractError, match="requires a string"):
        definition.validate_slot_bindings({"main_media": asset_id, "hook": 123})
    with pytest.raises(TemplateContractError, match="Content Forge asset ID"):
        definition.validate_slot_bindings(
            {"main_media": "/tmp/raw.mp4", "hook": "not a raw-path contract"}
        )
    with pytest.raises(TemplateContractError, match="JSON object with string keys"):
        definition.validate_slot_bindings({1: "not-a-string-key"})  # type: ignore[arg-type]
    with pytest.raises(TemplateContractError, match="JSON object with string keys"):
        definition.validate_slot_bindings(
            {"main_media": asset_id, "hook": object()}  # type: ignore[dict-item]
        )


def test_template_asset_paths_are_canonical_package_relative_paths() -> None:
    kwargs = {
        "asset_id": "fixture",
        "sha256": "c" * 64,
        "license_spdx": "Apache-2.0",
        "media_type": "image_svg",
    }
    for path in (
        "../outside.svg",
        "/absolute.svg",
        ".",
        "assets\\frame.svg",
        "C:/frame.svg",
    ):
        with pytest.raises(ValueError, match="template asset path"):
            TemplateAssetDefinition(relative_path=path, **kwargs)


def test_registries_are_exact_versioned_and_reject_collisions() -> None:
    bundle = create_builtin_registries()

    assert bundle.components.get("media", BUILTIN_COMPONENT_VERSION) == MEDIA_COMPONENT
    assert (
        bundle.templates.get(HOOK_OVERLAY_TEMPLATE_ID, HOOK_OVERLAY_TEMPLATE_VERSION)
        .definition
        == hook_overlay_definition()
    )

    with pytest.raises(DuplicateRegistryEntryError, match="component already registered"):
        bundle.components.register(MEDIA_COMPONENT)
    with pytest.raises(UnknownRegistryEntryError, match="unknown template"):
        bundle.templates.get(HOOK_OVERLAY_TEMPLATE_ID, "999")


def test_template_registration_requires_all_referenced_components() -> None:
    assets = TemplateAssetRegistry()
    components = ComponentRegistry()
    skins = SkinRegistry(assets)
    templates = TemplateRegistry(components, skins)
    definition = TemplateDefinition(
        template_id="future_template",
        version="1.0",
        description="Future test template.",
        components=(ComponentRef(component_id="ghost", version="1.0"),),
        slots=(
            TemplateSlot(
                slot_id="main",
                slot_kind="component",
                component=ComponentRef(component_id="ghost", version="1.0"),
            ),
        ),
    )

    with pytest.raises(RegistryReferenceError, match="unknown component ghost@1.0"):
        templates.register(
            TemplateRegistration(
                definition=definition,
                resolver=lambda project, assets, **kwargs: ResolvedTemplate(
                    template_id="future_template",
                    version="1.0",
                ),
            )
        )


def test_skin_registry_rejects_nonredistributable_packaged_assets() -> None:
    assets = TemplateAssetRegistry()
    assets.register(
        TemplateAssetDefinition(
            asset_id="private_fixture",
            relative_path="assets/private.svg",
            sha256="b" * 64,
            license_spdx="Proprietary",
            redistributable=False,
            media_type="image_svg",
        )
    )
    skins = SkinRegistry(assets)

    with pytest.raises(RegistryReferenceError, match="non-redistributable"):
        skins.register(
            SkinDefinition(
                skin_id="bad_skin",
                version="1.0",
                asset_ids=("private_fixture",),
                description="Must not enter a reusable public skin.",
            )
        )


def test_builtin_neutral_skin_asset_is_packaged_and_digest_bound() -> None:
    assert NEUTRAL_FRAME_ASSET.redistributable is True
    assert NEUTRAL_FRAME_ASSET.license_spdx == "Apache-2.0"
    assert NEUTRAL_FRAME_ASSET.asset_id in NEUTRAL_SKIN.asset_ids

    path = files("content_forge.templates")
    for part in NEUTRAL_FRAME_ASSET.relative_path.split("/"):
        path = path.joinpath(part)
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == NEUTRAL_FRAME_ASSET_SHA256


def test_registered_compile_preserves_legacy_semantics_and_adds_registry_evidence() -> None:
    asset = _image_asset()
    project = _hook_project(asset)
    assets = {asset.asset_id: asset}
    variant_id = project.variants[0].variant_id

    legacy = compile_hook_overlay_legacy(
        project,
        assets,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
        variant_id=variant_id,
    )
    registered = compile_registered_template(
        project,
        assets,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
        variant_id=variant_id,
    )
    public = compile_hook_overlay(
        project,
        assets,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
        variant_id=variant_id,
    )

    assert public == registered
    registered_properties = dict(registered.template_properties)
    evidence_json = registered_properties.pop(REGISTRY_EVIDENCE_PROPERTY)
    assert isinstance(evidence_json, str)
    evidence = json.loads(evidence_json)
    assert registered_properties == dict(legacy.template_properties)
    assert registered.validated_copy(
        update={"template_properties": registered_properties}
    ) == legacy

    assert evidence["schema_version"] == 1
    assert evidence["template"]["template_id"] == HOOK_OVERLAY_TEMPLATE_ID
    assert evidence["template"]["version"] == HOOK_OVERLAY_TEMPLATE_VERSION
    assert len(evidence["template"]["definition_sha256"]) == 64
    assert {
        (item["component_id"], item["version"])
        for item in evidence["components"]
    } == {
        ("media", BUILTIN_COMPONENT_VERSION),
        ("text", BUILTIN_COMPONENT_VERSION),
        ("original_audio", BUILTIN_COMPONENT_VERSION),
    }
    assert all(len(item["definition_sha256"]) == 64 for item in evidence["components"])
    assert evidence["skins"] == []
    assert render_plan_digest(registered) != render_plan_digest(legacy)


def test_registry_compile_fails_closed_for_unregistered_exact_version() -> None:
    asset = _image_asset()
    project = _hook_project(asset, version="999")

    with pytest.raises(UnknownRegistryEntryError, match="hook_overlay@999"):
        compile_registered_template(
            project,
            {asset.asset_id: asset},
            profile_id=SHORTS_PREVIEW_PROFILE_ID,
            variant_id=project.variants[0].variant_id,
        )


def _single_component_registry() -> tuple[ComponentRegistry, SkinRegistry, TemplateRegistry]:
    assets = TemplateAssetRegistry()
    components = ComponentRegistry()
    components.register(
        ComponentDefinition(
            component_id="noop",
            version="1.0",
            output_kind="overlay",
            description="No-op test component.",
        )
    )
    skins = SkinRegistry(assets)
    return components, skins, TemplateRegistry(components, skins)


def _declared_noop_definition() -> TemplateDefinition:
    return TemplateDefinition(
        template_id="declared",
        version="1.0",
        description="Identity binding test.",
        components=(ComponentRef(component_id="noop", version="1.0"),),
        slots=(),
    )


def _declared_project(*, variants: tuple[Variant, ...] = ()) -> Project:
    return Project(
        content_kind="test",
        template=TemplateRef(template_id="declared", version="1.0"),
        variants=variants,
        scenes=(Scene(order=0, duration_seconds=1.0),),
        output_profiles=(shorts_preview_profile(),),
    )


def test_registry_rejects_resolver_identity_confusion() -> None:
    _, _, templates = _single_component_registry()
    templates.register(
        TemplateRegistration(
            definition=_declared_noop_definition(),
            resolver=lambda project, assets, **kwargs: ResolvedTemplate(
                template_id="other",
                version="1.0",
            ),
        )
    )

    with pytest.raises(TemplateResolutionRegistryError, match="mismatched template identity"):
        templates.resolve(_declared_project(), {})


def test_registry_requires_resolver_to_bind_selected_profile_and_variant() -> None:
    _, _, templates = _single_component_registry()
    templates.register(
        TemplateRegistration(
            definition=_declared_noop_definition(),
            resolver=lambda project, assets, **kwargs: ResolvedTemplate(
                template_id="declared",
                version="1.0",
            ),
        )
    )

    with pytest.raises(
        TemplateResolutionRegistryError,
        match="did not bind the selected output profile",
    ):
        templates.resolve(_declared_project(), {})


def test_registry_selects_variant_before_resolver_and_rejects_ambiguity() -> None:
    _, _, templates = _single_component_registry()
    called = False

    def resolver(project, assets, *, profile_id=None, variant_id=None):
        nonlocal called
        called = True
        return ResolvedTemplate(
            template_id="declared",
            version="1.0",
            properties={
                "resolved_profile_id": profile_id,
                "resolved_variant_id": variant_id,
            },
        )

    templates.register(
        TemplateRegistration(
            definition=_declared_noop_definition(),
            resolver=resolver,
        )
    )
    project = _declared_project(variants=(Variant(), Variant()))

    with pytest.raises(TemplateResolutionRegistryError, match="variant_id is required"):
        templates.resolve(project, {})
    assert called is False


class _FakeDistribution:
    metadata = {"Name": "fixture-plugin"}
    version = "2.3"


class _FakeEntryPoint:
    def __init__(self, group: str, name: str, value: str) -> None:
        self.group = group
        self.name = name
        self.value = value
        self.dist = _FakeDistribution()
        self.loaded = False

    def load(self):
        self.loaded = True
        raise AssertionError("PR11 discovery must not execute plugin code")


def test_plugin_discovery_is_metadata_only_and_deterministic() -> None:
    component = _FakeEntryPoint(
        COMPONENT_ENTRY_POINT_GROUP,
        "z_component",
        "example.components:register",
    )
    template = _FakeEntryPoint(
        TEMPLATE_ENTRY_POINT_GROUP,
        "a_template",
        "example.templates:register",
    )
    ignored = _FakeEntryPoint(
        "unrelated.group",
        "ignored",
        "example:ignored",
    )

    discovered = discover_plugin_candidates((component, ignored, template))

    assert [(item.group, item.name) for item in discovered] == [
        (COMPONENT_ENTRY_POINT_GROUP, "z_component"),
        (TEMPLATE_ENTRY_POINT_GROUP, "a_template"),
    ]
    assert all(item.distribution == "fixture-plugin" for item in discovered)
    assert all(item.distribution_version == "2.3" for item in discovered)
    assert component.loaded is False
    assert template.loaded is False
    assert ignored.loaded is False


def test_template_schema_allows_safe_anchor_geometry_without_renderer_dependencies() -> None:
    definition = TemplateDefinition(
        template_id="simple_card",
        version="1.0",
        description="Declarative-only schema smoke test.",
        components=(ComponentRef(component_id="text", version="1.0"),),
        anchors=(
            TemplateAnchor(
                anchor_id="top_center",
                point=NormalizedPoint(x=0.5, y=0.1),
            ),
        ),
        slots=(
            TemplateSlot(
                slot_id="title",
                slot_kind="text",
                component=ComponentRef(component_id="text", version="1.0"),
                rect=NormalizedRect(x=0.1, y=0.05, width=0.8, height=0.2),
                anchor_id="top_center",
            ),
        ),
    )

    assert definition.model_dump(mode="json")["slots"][0]["slot_id"] == "title"
