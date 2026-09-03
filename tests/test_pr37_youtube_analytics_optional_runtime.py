from __future__ import annotations

import pytest


def test_pr37_optional_google_runtime_imports_and_read_only_scopes():
    pytest.importorskip("googleapiclient.discovery")
    pytest.importorskip("google_auth_oauthlib.flow")

    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    from content_forge.providers import (
        YOUTUBE_ANALYTICS_OAUTH_SCOPES,
        YouTubeAnalyticsConfig,
        YouTubeAnalyticsProvider,
    )
    from content_forge.providers.youtube_analytics_auth import authorize_youtube_analytics

    assert callable(build)
    assert InstalledAppFlow.__name__ == "InstalledAppFlow"
    assert callable(authorize_youtube_analytics)
    assert YouTubeAnalyticsConfig.__name__ == "YouTubeAnalyticsConfig"
    assert YouTubeAnalyticsProvider.__name__ == "YouTubeAnalyticsProvider"
    assert YOUTUBE_ANALYTICS_OAUTH_SCOPES == (
        "https://www.googleapis.com/auth/yt-analytics.readonly",
        "https://www.googleapis.com/auth/youtube.readonly",
    )
