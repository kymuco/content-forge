"""High-level FFmpeg backend facade."""

from __future__ import annotations

from pathlib import Path

from content_forge.timeline import RenderPlan

from .compiler import AssetPathSource, compile_ffmpeg_command
from .models import FFmpegCapabilities, RenderCommandManifest, RenderResult
from .runner import CancellationToken, execute_ffmpeg


class FFmpegBackend:
    """Compile and execute validated RenderPlan instances with one capability snapshot."""

    def __init__(
        self,
        capabilities: FFmpegCapabilities,
        asset_paths: AssetPathSource,
        *,
        prefer_nvenc: bool = True,
    ) -> None:
        self.capabilities = capabilities
        self.asset_paths = asset_paths
        self.prefer_nvenc = prefer_nvenc

    def compile(
        self,
        plan: RenderPlan,
        output_path: str | Path,
    ) -> RenderCommandManifest:
        return compile_ffmpeg_command(
            plan,
            self.asset_paths,
            self.capabilities,
            output_path,
            prefer_nvenc=self.prefer_nvenc,
        )

    def render(
        self,
        plan: RenderPlan,
        output_path: str | Path,
        *,
        cancellation: CancellationToken | None = None,
        timeout: float | None = None,
    ) -> RenderResult:
        manifest = self.compile(plan, output_path)
        return execute_ffmpeg(
            manifest,
            cancellation=cancellation,
            timeout=timeout,
        )
