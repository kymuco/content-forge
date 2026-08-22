"""Deterministic normalized-to-pixel geometry for FFmpeg plans."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from content_forge.core import NormalizedRect, OutputProfile

from .models import PixelRect


class RenderGeometryError(ValueError):
    pass


def _edge(value: float, extent: int) -> int:
    scaled = Decimal(str(value)) * Decimal(extent)
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def resolve_pixel_rect(rect: NormalizedRect, profile: OutputProfile) -> PixelRect:
    """Resolve normalized rectangle edges using stable half-up rounding.

    Edges are rounded independently rather than rounding width/height in isolation. That
    keeps adjacent normalized slots aligned and prevents one-pixel drift between preview
    and final profiles.
    """

    left = max(0, min(profile.width, _edge(rect.x, profile.width)))
    top = max(0, min(profile.height, _edge(rect.y, profile.height)))
    right = max(
        0,
        min(profile.width, _edge(rect.x + rect.width, profile.width)),
    )
    bottom = max(
        0,
        min(profile.height, _edge(rect.y + rect.height, profile.height)),
    )
    if right <= left or bottom <= top:
        raise RenderGeometryError(
            "normalized rectangle collapses after output-profile pixel rounding"
        )
    return PixelRect(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
    )
