"""Built-in vertical output profiles for the first Shorts workflow."""

from __future__ import annotations

from content_forge.core import NormalizedRect, OutputProfile, SafeZone

SHORTS_PREVIEW_PROFILE_ID = "shorts_preview"
SHORTS_FINAL_PROFILE_ID = "shorts_final"

# Safe zones describe platform-UI regions that presentation templates should avoid for
# important text. Media may still extend underneath them.
SHORTS_SAFE_ZONES = (
    SafeZone(
        name="top_ui",
        rect=NormalizedRect(x=0.0, y=0.0, width=1.0, height=0.045),
    ),
    SafeZone(
        name="right_ui",
        rect=NormalizedRect(x=0.88, y=0.22, width=0.12, height=0.58),
    ),
    SafeZone(
        name="bottom_ui",
        rect=NormalizedRect(x=0.0, y=0.88, width=1.0, height=0.12),
    ),
)


def shorts_preview_profile(*, fps: float = 30.0) -> OutputProfile:
    """Return the low-latency vertical review profile."""

    return OutputProfile(
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
        width=540,
        height=960,
        fps=fps,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        safe_zones=SHORTS_SAFE_ZONES,
        properties={"purpose": "preview", "orientation": "vertical"},
    )


def shorts_final_profile(*, fps: float = 30.0) -> OutputProfile:
    """Return the initial 1080x1920 final vertical profile."""

    return OutputProfile(
        profile_id=SHORTS_FINAL_PROFILE_ID,
        width=1080,
        height=1920,
        fps=fps,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        safe_zones=SHORTS_SAFE_ZONES,
        properties={"purpose": "final", "orientation": "vertical"},
    )
