"""Declarative template, component, skin, and packaged-asset contracts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, field_validator, model_validator

from content_forge.core import (
    EntityKind,
    NormalizedPoint,
    NormalizedRect,
    RegistryKey,
    require_entity_id,
)
from content_forge.core.models import FrozenModel, SHA256

ComponentOutputKind = Literal["scene", "overlay", "audio", "motion", "transition"]
TemplateSlotKind = Literal["media", "text", "asset", "component"]
SafeZonePolicy = Literal["avoid", "contain", "reserve"]
_SLOT_BINDINGS_ADAPTER = TypeAdapter(dict[str, JsonValue])


class TemplateContractError(ValueError):
    """Raised when declarative template inputs do not satisfy a template contract."""


class ComponentRef(FrozenModel):
    component_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)


class SkinRef(FrozenModel):
    skin_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)


class ComponentDefinition(FrozenModel):
    """Versioned reusable component contract independent of a concrete renderer."""

    component_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)
    output_kind: ComponentOutputKind
    accepts_text: bool = False
    accepts_asset: bool = False
    required_properties: tuple[RegistryKey, ...] = ()
    property_defaults: Mapping[str, JsonValue] = Field(default_factory=dict)
    description: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_properties(self) -> Self:
        if len(self.required_properties) != len(set(self.required_properties)):
            raise ValueError("duplicate required component property")
        return self


class TemplateAnchor(FrozenModel):
    anchor_id: RegistryKey
    point: NormalizedPoint


class TemplateSafeZone(FrozenModel):
    zone_id: RegistryKey
    rect: NormalizedRect
    policy: SafeZonePolicy = "avoid"
    description: str | None = Field(default=None, max_length=2048)


class TemplateSlot(FrozenModel):
    """One declared input/layout slot in a template."""

    slot_id: RegistryKey
    slot_kind: TemplateSlotKind
    component: ComponentRef
    required: bool = True
    rect: NormalizedRect | None = None
    anchor_id: RegistryKey | None = None
    description: str | None = Field(default=None, max_length=2048)


class TemplateDefault(FrozenModel):
    key: RegistryKey
    value: JsonValue


class TemplateAssetDefinition(FrozenModel):
    """Project-owned packaged asset that may be referenced by reusable skins."""

    asset_id: RegistryKey
    relative_path: str = Field(min_length=1, max_length=512)
    sha256: SHA256
    license_spdx: str = Field(min_length=1, max_length=128)
    redistributable: bool = True
    media_type: RegistryKey

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("template asset path must use canonical POSIX separators")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or value != path.as_posix()
            or any(part in {".", ".."} or ":" in part for part in path.parts)
        ):
            raise ValueError("template asset path must be a safe canonical relative package path")
        return value


class SkinDefinition(FrozenModel):
    """Versioned reusable style/asset bundle."""

    skin_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)
    properties: Mapping[str, JsonValue] = Field(default_factory=dict)
    asset_ids: tuple[RegistryKey, ...] = ()
    description: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_asset_ids(self) -> Self:
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("duplicate skin asset ID")
        return self


class TemplateDefinition(FrozenModel):
    """Declarative, versioned template schema registered independently from its resolver."""

    template_id: RegistryKey
    version: str = Field(min_length=1, max_length=64)
    slots: tuple[TemplateSlot, ...]
    anchors: tuple[TemplateAnchor, ...] = ()
    safe_zones: tuple[TemplateSafeZone, ...] = ()
    components: tuple[ComponentRef, ...] = ()
    skins: tuple[SkinRef, ...] = ()
    defaults: tuple[TemplateDefault, ...] = ()
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)
    description: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        slot_ids = [slot.slot_id for slot in self.slots]
        anchor_ids = [anchor.anchor_id for anchor in self.anchors]
        zone_ids = [zone.zone_id for zone in self.safe_zones]
        component_keys = [
            (component.component_id, component.version) for component in self.components
        ]
        skin_keys = [(skin.skin_id, skin.version) for skin in self.skins]
        default_keys = [default.key for default in self.defaults]

        for values, label in (
            (slot_ids, "template slot ID"),
            (anchor_ids, "template anchor ID"),
            (zone_ids, "template safe-zone ID"),
            (component_keys, "template component reference"),
            (skin_keys, "template skin reference"),
            (default_keys, "template default key"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label}")

        known_components = set(component_keys)
        known_anchors = set(anchor_ids)
        for slot in self.slots:
            key = (slot.component.component_id, slot.component.version)
            if key not in known_components:
                raise ValueError(
                    f"slot {slot.slot_id!r} references an undeclared component"
                )
            if slot.anchor_id is not None and slot.anchor_id not in known_anchors:
                raise ValueError(
                    f"slot {slot.slot_id!r} references an unknown anchor"
                )

        return self

    def validate_slot_bindings(
        self,
        bindings: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Validate one bounded JSON slot-binding object without executing template code."""

        try:
            validated = _SLOT_BINDINGS_ADAPTER.validate_python(bindings, strict=True)
        except (ValidationError, TypeError, ValueError) as exc:
            raise TemplateContractError(
                "template slot bindings must be a JSON object with string keys"
            ) from exc

        slots = {slot.slot_id: slot for slot in self.slots}
        unknown = sorted(set(validated) - set(slots))
        if unknown:
            raise TemplateContractError(
                "unknown template slot binding(s): " + ", ".join(unknown)
            )

        missing = sorted(
            slot.slot_id
            for slot in self.slots
            if slot.required and slot.slot_id not in validated
        )
        if missing:
            raise TemplateContractError(
                "missing required template slot binding(s): " + ", ".join(missing)
            )

        for slot_id, value in validated.items():
            slot = slots[slot_id]
            if slot.slot_kind == "text" and not isinstance(value, str):
                raise TemplateContractError(
                    f"template text slot {slot_id!r} requires a string"
                )
            if slot.slot_kind in {"media", "asset"}:
                if not isinstance(value, str):
                    raise TemplateContractError(
                        f"template {slot.slot_kind} slot {slot_id!r} requires an asset identity string"
                    )
                try:
                    require_entity_id(value, EntityKind.ASSET)
                except ValueError as exc:
                    raise TemplateContractError(
                        f"template {slot.slot_kind} slot {slot_id!r} requires a Content Forge asset ID"
                    ) from exc
        return validated
