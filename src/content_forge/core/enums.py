"""Closed domain enumerations.

Extensible identifiers such as content kinds, templates, workflows and components are
registry keys rather than enums so plugins can add new values without changing core.
"""

from enum import StrEnum


class ProjectState(StrEnum):
    INBOX = "inbox"
    DRAFT = "draft"
    PREPARED = "prepared"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    RENDERING = "rendering"
    QC = "qc"
    DONE = "done"


class MediaType(StrEnum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    OTHER = "other"


class PermissionStatus(StrEnum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    LICENSED = "licensed"
    PUBLIC_DOMAIN = "public_domain"
    RESTRICTED = "restricted"
    DENIED = "denied"


class FitMode(StrEnum):
    COVER = "cover"
    CONTAIN = "contain"
    STRETCH = "stretch"
    BLUR_BACKGROUND = "blur_background"


class ReviewStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class ReviewPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    BLOCKING = "blocking"


class AttentionMode(StrEnum):
    AUTO = "auto"
    REVIEW = "review"
    MANUAL = "manual"
