"""Metadata-only plugin discovery boundary for future third-party extensions.

PR11 deliberately discovers candidate entry points without importing or executing them.
Loading, trust policy, isolation, compatibility negotiation, and installation UX remain
future work.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from importlib import metadata
from typing import Protocol

from pydantic import Field

from content_forge.core import RegistryKey
from content_forge.core.models import FrozenModel

TEMPLATE_ENTRY_POINT_GROUP = "content_forge.templates"
COMPONENT_ENTRY_POINT_GROUP = "content_forge.components"
SKIN_ENTRY_POINT_GROUP = "content_forge.skins"
PLUGIN_ENTRY_POINT_GROUPS = frozenset(
    {
        TEMPLATE_ENTRY_POINT_GROUP,
        COMPONENT_ENTRY_POINT_GROUP,
        SKIN_ENTRY_POINT_GROUP,
    }
)


class EntryPointLike(Protocol):
    group: str
    name: str
    value: str


class PluginCandidate(FrozenModel):
    group: RegistryKey
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=1024)
    distribution: str | None = Field(default=None, max_length=512)
    distribution_version: str | None = Field(default=None, max_length=128)


def _distribution_metadata(entry_point: object) -> tuple[str | None, str | None]:
    distribution = getattr(entry_point, "dist", None)
    if distribution is None:
        return None, None

    name: str | None = None
    metadata_object = getattr(distribution, "metadata", None)
    if metadata_object is not None:
        getter = getattr(metadata_object, "get", None)
        if callable(getter):
            candidate = getter("Name")
            if candidate is not None:
                name = str(candidate)

    version_value = getattr(distribution, "version", None)
    version = None if version_value is None else str(version_value)
    return name, version


def _installed_entry_points() -> Iterator[EntryPointLike]:
    """Normalize importlib.metadata entry-point APIs across supported Python versions."""

    discovered = metadata.entry_points()
    selector = getattr(discovered, "select", None)
    if callable(selector):
        for group in sorted(PLUGIN_ENTRY_POINT_GROUPS):
            yield from selector(group=group)
        return

    # Compatibility with the older mapping-shaped API. This path is intentionally kept
    # metadata-only as well; values are yielded without importing/loading entry points.
    if isinstance(discovered, Mapping):
        for group in sorted(PLUGIN_ENTRY_POINT_GROUPS):
            yield from discovered.get(group, ())
        return

    # A future iterable-only API is still safe to inspect as metadata. Filtering happens
    # in discover_plugin_candidates below.
    yield from discovered


def discover_plugin_candidates(
    entry_points: Iterable[EntryPointLike] | None = None,
) -> tuple[PluginCandidate, ...]:
    """Return deterministic entry-point metadata without ever calling ``load()``."""

    source = _installed_entry_points() if entry_points is None else entry_points
    candidates: list[PluginCandidate] = []
    for entry_point in source:
        group = str(entry_point.group)
        if group not in PLUGIN_ENTRY_POINT_GROUPS:
            continue
        distribution, distribution_version = _distribution_metadata(entry_point)
        candidates.append(
            PluginCandidate(
                group=group,
                name=str(entry_point.name),
                value=str(entry_point.value),
                distribution=distribution,
                distribution_version=distribution_version,
            )
        )

    candidates.sort(
        key=lambda item: (
            item.group,
            item.name,
            item.value,
            item.distribution or "",
            item.distribution_version or "",
        )
    )
    return tuple(candidates)
