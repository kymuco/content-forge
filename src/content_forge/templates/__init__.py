"""Built-in presentation templates."""

from .hook_overlay import (
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    HookOverlayAssetResolver,
    HookOverlayConfig,
    HookOverlayTemplateError,
    compile_hook_overlay,
    resolve_hook_overlay,
)

__all__ = [
    "HOOK_OVERLAY_TEMPLATE_ID",
    "HOOK_OVERLAY_TEMPLATE_VERSION",
    "HookOverlayAssetResolver",
    "HookOverlayConfig",
    "HookOverlayTemplateError",
    "compile_hook_overlay",
    "resolve_hook_overlay",
]
