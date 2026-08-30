"""Versioned registries and the generic template resolution/compile path."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from content_forge.core import Asset, Project
from content_forge.timeline import AssetResolver, RenderPlan, ResolvedTemplate, compile_timeline

from .contracts import (
    ComponentDefinition,
    ComponentRef,
    SkinDefinition,
    SkinRef,
    TemplateAssetDefinition,
    TemplateDefinition,
)

TemplateAssetSource = Mapping[str, Asset] | AssetResolver
REGISTRY_EVIDENCE_PROPERTY = "content_forge_registry_evidence_v1"


class TemplateResolver(Protocol):
    def __call__(
        self,
        project: Project,
        assets: TemplateAssetSource,
        *,
        profile_id: str | None = None,
        variant_id: str | None = None,
    ) -> ResolvedTemplate: ...


class RegistryError(ValueError):
    """Base class for registry contract failures."""


class DuplicateRegistryEntryError(RegistryError):
    pass


class UnknownRegistryEntryError(RegistryError):
    pass


class RegistryReferenceError(RegistryError):
    pass


class TemplateResolutionRegistryError(RegistryError):
    pass


@dataclass(frozen=True, slots=True)
class TemplateRegistration:
    definition: TemplateDefinition
    resolver: TemplateResolver


class ComponentRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ComponentDefinition] = {}

    def register(self, definition: ComponentDefinition) -> None:
        key = (definition.component_id, definition.version)
        if key in self._items:
            raise DuplicateRegistryEntryError(
                f"component already registered: {definition.component_id}@{definition.version}"
            )
        self._items[key] = definition

    def get(self, component_id: str, version: str) -> ComponentDefinition:
        try:
            return self._items[(component_id, version)]
        except KeyError as exc:
            raise UnknownRegistryEntryError(
                f"unknown component: {component_id}@{version}"
            ) from exc

    def definitions(self) -> tuple[ComponentDefinition, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


class TemplateAssetRegistry:
    def __init__(self) -> None:
        self._items: dict[str, TemplateAssetDefinition] = {}

    def register(self, definition: TemplateAssetDefinition) -> None:
        if definition.asset_id in self._items:
            raise DuplicateRegistryEntryError(
                f"template asset already registered: {definition.asset_id}"
            )
        self._items[definition.asset_id] = definition

    def get(self, asset_id: str) -> TemplateAssetDefinition:
        try:
            return self._items[asset_id]
        except KeyError as exc:
            raise UnknownRegistryEntryError(
                f"unknown template asset: {asset_id}"
            ) from exc

    def definitions(self) -> tuple[TemplateAssetDefinition, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


class SkinRegistry:
    def __init__(self, assets: TemplateAssetRegistry) -> None:
        self._assets = assets
        self._items: dict[tuple[str, str], SkinDefinition] = {}

    def register(self, definition: SkinDefinition) -> None:
        key = (definition.skin_id, definition.version)
        if key in self._items:
            raise DuplicateRegistryEntryError(
                f"skin already registered: {definition.skin_id}@{definition.version}"
            )
        for asset_id in definition.asset_ids:
            try:
                asset = self._assets.get(asset_id)
            except UnknownRegistryEntryError as exc:
                raise RegistryReferenceError(
                    f"skin {definition.skin_id}@{definition.version} references "
                    f"unknown packaged asset {asset_id}"
                ) from exc
            if not asset.redistributable:
                raise RegistryReferenceError(
                    f"skin {definition.skin_id}@{definition.version} references "
                    f"non-redistributable packaged asset {asset_id}"
                )
        self._items[key] = definition

    def get(self, skin_id: str, version: str) -> SkinDefinition:
        try:
            return self._items[(skin_id, version)]
        except KeyError as exc:
            raise UnknownRegistryEntryError(
                f"unknown skin: {skin_id}@{version}"
            ) from exc

    def asset_definitions(
        self,
        skin_id: str,
        version: str,
    ) -> tuple[TemplateAssetDefinition, ...]:
        skin = self.get(skin_id, version)
        return tuple(self._assets.get(asset_id) for asset_id in skin.asset_ids)

    def definitions(self) -> tuple[SkinDefinition, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


class TemplateRegistry:
    def __init__(
        self,
        components: ComponentRegistry,
        skins: SkinRegistry,
    ) -> None:
        self._components = components
        self._skins = skins
        self._items: dict[tuple[str, str], TemplateRegistration] = {}

    def register(self, registration: TemplateRegistration) -> None:
        definition = registration.definition
        key = (definition.template_id, definition.version)
        if key in self._items:
            raise DuplicateRegistryEntryError(
                f"template already registered: {definition.template_id}@{definition.version}"
            )

        for component in definition.components:
            self._require_component(component, definition)
        for slot in definition.slots:
            component = self._require_component(slot.component, definition)
            if slot.slot_kind == "text" and not component.accepts_text:
                raise RegistryReferenceError(
                    f"template {definition.template_id}@{definition.version} text slot "
                    f"{slot.slot_id!r} references component "
                    f"{component.component_id}@{component.version} without text capability"
                )
            if slot.slot_kind in {"media", "asset"} and not component.accepts_asset:
                raise RegistryReferenceError(
                    f"template {definition.template_id}@{definition.version} "
                    f"{slot.slot_kind} slot {slot.slot_id!r} references component "
                    f"{component.component_id}@{component.version} without asset capability"
                )
        for skin in definition.skins:
            self._require_skin(skin, definition)

        self._items[key] = registration

    def _require_component(
        self,
        reference: ComponentRef,
        template: TemplateDefinition,
    ) -> ComponentDefinition:
        try:
            return self._components.get(reference.component_id, reference.version)
        except UnknownRegistryEntryError as exc:
            raise RegistryReferenceError(
                f"template {template.template_id}@{template.version} references "
                f"unknown component {reference.component_id}@{reference.version}"
            ) from exc

    def _require_skin(
        self,
        reference: SkinRef,
        template: TemplateDefinition,
    ) -> SkinDefinition:
        try:
            return self._skins.get(reference.skin_id, reference.version)
        except UnknownRegistryEntryError as exc:
            raise RegistryReferenceError(
                f"template {template.template_id}@{template.version} references "
                f"unknown skin {reference.skin_id}@{reference.version}"
            ) from exc

    def get(self, template_id: str, version: str) -> TemplateRegistration:
        try:
            return self._items[(template_id, version)]
        except KeyError as exc:
            raise UnknownRegistryEntryError(
                f"unknown template: {template_id}@{version}"
            ) from exc

    def definitions(self) -> tuple[TemplateDefinition, ...]:
        return tuple(
            self._items[key].definition
            for key in sorted(self._items)
        )

    @staticmethod
    def _select_profile_id(project: Project, profile_id: str | None) -> str:
        if profile_id is not None:
            if any(profile.profile_id == profile_id for profile in project.output_profiles):
                return profile_id
            raise TemplateResolutionRegistryError(
                f"unknown output profile: {profile_id}"
            )
        if len(project.output_profiles) != 1:
            raise TemplateResolutionRegistryError(
                "profile_id is required unless the project has exactly one output profile"
            )
        return project.output_profiles[0].profile_id

    @staticmethod
    def _select_variant_id(project: Project, variant_id: str | None) -> str | None:
        if variant_id is not None:
            if any(variant.variant_id == variant_id for variant in project.variants):
                return variant_id
            raise TemplateResolutionRegistryError(f"unknown variant: {variant_id}")
        if not project.variants:
            return None
        if len(project.variants) == 1:
            return project.variants[0].variant_id
        raise TemplateResolutionRegistryError(
            "variant_id is required when the project has more than one variant"
        )

    @staticmethod
    def _definition_digest(definition: object) -> str:
        model_dump = getattr(definition, "model_dump", None)
        if not callable(model_dump):
            raise TypeError("registry definition must support canonical model dumping")
        payload = model_dump(mode="json")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _registry_evidence(self, definition: TemplateDefinition) -> str:
        components = []
        for reference in sorted(
            definition.components,
            key=lambda item: (item.component_id, item.version),
        ):
            component = self._components.get(reference.component_id, reference.version)
            components.append(
                {
                    "component_id": reference.component_id,
                    "version": reference.version,
                    "definition_sha256": self._definition_digest(component),
                }
            )

        skins: list[dict[str, object]] = []
        for reference in sorted(
            definition.skins,
            key=lambda item: (item.skin_id, item.version),
        ):
            skin = self._skins.get(reference.skin_id, reference.version)
            packaged_assets = [
                {
                    "asset_id": asset.asset_id,
                    "sha256": asset.sha256,
                    "license_spdx": asset.license_spdx,
                }
                for asset in sorted(
                    self._skins.asset_definitions(reference.skin_id, reference.version),
                    key=lambda item: item.asset_id,
                )
            ]
            skins.append(
                {
                    "skin_id": reference.skin_id,
                    "version": reference.version,
                    "definition_sha256": self._definition_digest(skin),
                    "packaged_assets": packaged_assets,
                }
            )

        evidence = {
            "schema_version": 1,
            "template": {
                "template_id": definition.template_id,
                "version": definition.version,
                "definition_sha256": self._definition_digest(definition),
            },
            "components": components,
            "skins": skins,
        }
        return json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _resolve_selected(
        self,
        project: Project,
        assets: TemplateAssetSource,
        registration: TemplateRegistration,
        *,
        profile_id: str,
        variant_id: str | None,
    ) -> ResolvedTemplate:
        resolved = registration.resolver(
            project,
            assets,
            profile_id=profile_id,
            variant_id=variant_id,
        )
        definition = registration.definition
        if (
            resolved.template_id != definition.template_id
            or resolved.version != definition.version
        ):
            raise TemplateResolutionRegistryError(
                "template resolver returned mismatched template identity"
            )

        if "resolved_profile_id" not in resolved.properties:
            raise TemplateResolutionRegistryError(
                "template resolver did not bind the selected output profile"
            )
        if resolved.properties["resolved_profile_id"] != profile_id:
            raise TemplateResolutionRegistryError(
                "template resolver returned mismatched output-profile binding"
            )
        if "resolved_variant_id" not in resolved.properties:
            raise TemplateResolutionRegistryError(
                "template resolver did not bind the selected variant"
            )
        if resolved.properties["resolved_variant_id"] != variant_id:
            raise TemplateResolutionRegistryError(
                "template resolver returned mismatched variant binding"
            )
        if REGISTRY_EVIDENCE_PROPERTY in resolved.properties:
            raise TemplateResolutionRegistryError(
                "template resolver attempted to set reserved registry evidence"
            )

        # FrozenModel deliberately stores nested JSON containers as immutable internal
        # values. Use its JSON serializer to deep-thaw valid resolver metadata before
        # adding registry evidence and revalidating the complete ResolvedTemplate.
        serialized = resolved.model_dump(mode="json")
        properties = serialized.get("properties")
        if not isinstance(properties, dict):
            raise TemplateResolutionRegistryError(
                "template resolver returned invalid JSON properties"
            )
        properties[REGISTRY_EVIDENCE_PROPERTY] = self._registry_evidence(definition)
        return ResolvedTemplate.model_validate(serialized)

    def resolve(
        self,
        project: Project,
        assets: TemplateAssetSource,
        *,
        profile_id: str | None = None,
        variant_id: str | None = None,
    ) -> ResolvedTemplate:
        if project.template is None:
            raise TemplateResolutionRegistryError("project has no template reference")
        registration = self.get(
            project.template.template_id,
            project.template.version,
        )
        selected_profile_id = self._select_profile_id(project, profile_id)
        selected_variant_id = self._select_variant_id(project, variant_id)
        return self._resolve_selected(
            project,
            assets,
            registration,
            profile_id=selected_profile_id,
            variant_id=selected_variant_id,
        )

    def compile(
        self,
        project: Project,
        assets: TemplateAssetSource,
        *,
        profile_id: str | None = None,
        variant_id: str | None = None,
    ) -> RenderPlan:
        if project.template is None:
            raise TemplateResolutionRegistryError("project has no template reference")
        registration = self.get(
            project.template.template_id,
            project.template.version,
        )
        selected_profile_id = self._select_profile_id(project, profile_id)
        selected_variant_id = self._select_variant_id(project, variant_id)
        resolved = self._resolve_selected(
            project,
            assets,
            registration,
            profile_id=selected_profile_id,
            variant_id=selected_variant_id,
        )
        return compile_timeline(
            project,
            assets,
            profile_id=selected_profile_id,
            variant_id=selected_variant_id,
            template=resolved,
        )


@dataclass(frozen=True, slots=True)
class RegistryBundle:
    assets: TemplateAssetRegistry
    components: ComponentRegistry
    skins: SkinRegistry
    templates: TemplateRegistry
