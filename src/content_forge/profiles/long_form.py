"""Built-in horizontal output profiles for long-form rendering."""

from __future__ import annotations

from content_forge.core import OutputProfile

LONG_FORM_1080P_PROFILE_ID = "long_form_1080p"
LONG_FORM_1440P_PROFILE_ID = "long_form_1440p"


def long_form_1080p_profile(*, fps: float = 30.0) -> OutputProfile:
    """Return the canonical 16:9 1920x1080 long-form final profile."""

    return OutputProfile(
        profile_id=LONG_FORM_1080P_PROFILE_ID,
        width=1920,
        height=1080,
        fps=fps,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        video_bitrate_kbps=12000,
        audio_bitrate_kbps=192,
        properties={
            "purpose": "final",
            "orientation": "horizontal",
            "format_family": "long_form",
            "resolution": "1080p",
            "aspect_ratio": "16:9",
        },
    )


def long_form_1440p_profile(*, fps: float = 30.0) -> OutputProfile:
    """Return the canonical 16:9 2560x1440 long-form final profile."""

    return OutputProfile(
        profile_id=LONG_FORM_1440P_PROFILE_ID,
        width=2560,
        height=1440,
        fps=fps,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        video_bitrate_kbps=24000,
        audio_bitrate_kbps=192,
        properties={
            "purpose": "final",
            "orientation": "horizontal",
            "format_family": "long_form",
            "resolution": "1440p",
            "aspect_ratio": "16:9",
        },
    )


__all__ = [
    "LONG_FORM_1080P_PROFILE_ID",
    "LONG_FORM_1440P_PROFILE_ID",
    "long_form_1080p_profile",
    "long_form_1440p_profile",
]
