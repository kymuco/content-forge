"""Deterministic renderer-independent timeline compilation."""

from .compiler import (
    AssetResolver,
    MissingTimelineAssetError,
    TemplateResolutionError,
    TimelineBoundsError,
    TimelineCompileError,
    TimelineSelectionError,
    TimelineTransitionError,
    compile_timeline,
    render_plan_digest,
)
from .models import (
    RENDER_PLAN_VERSION,
    TIMELINE_COMPILER_VERSION,
    PlannedAsset,
    PlannedAudioTrack,
    PlannedOverlay,
    PlannedScene,
    PlannedTransition,
    RenderPlan,
    ResolvedTemplate,
)

__all__ = [
    "AssetResolver",
    "MissingTimelineAssetError",
    "PlannedAsset",
    "PlannedAudioTrack",
    "PlannedOverlay",
    "PlannedScene",
    "PlannedTransition",
    "RENDER_PLAN_VERSION",
    "RenderPlan",
    "ResolvedTemplate",
    "TIMELINE_COMPILER_VERSION",
    "TemplateResolutionError",
    "TimelineBoundsError",
    "TimelineCompileError",
    "TimelineSelectionError",
    "TimelineTransitionError",
    "compile_timeline",
    "render_plan_digest",
]
