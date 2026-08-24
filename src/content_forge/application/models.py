"""Application-layer records for Inbox and local authentication."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import Field, StringConstraints, field_validator, model_validator

from content_forge.core import RegistryKey
from content_forge.core.models import FrozenModel

APP_ID = Annotated[
    str,
    StringConstraints(
        min_length=40,
        max_length=64,
        pattern=r"^cf_(?:intake|pair|session)_[0-9a-f]{32}$",
    ),
]
CONTENT_SHA256 = Annotated[
    str,
    StringConstraints(
        to_lower=True,
        strip_whitespace=True,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    ),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must be timezone-aware")
    return value


def new_app_id(kind: str) -> str:
    if kind not in {"intake", "pair", "session"}:
        raise ValueError(f"unsupported application ID kind: {kind}")
    return f"cf_{kind}_{uuid4().hex}"


class IntakeKind(StrEnum):
    FILE = "file"
    URL_NOTE = "url_note"


class IntakeState(StrEnum):
    RECEIVING = "receiving"
    PREPARED = "prepared"
    PARTIAL = "partial"
    FAILED = "failed"


class PreparationState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class InboxIntake(FrozenModel):
    intake_id: APP_ID = Field(default_factory=lambda: new_app_id("intake"))
    kind: IntakeKind
    state: IntakeState = IntakeState.RECEIVING
    original_name: str | None = Field(default=None, max_length=1024)
    mime_type: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0)
    content_sha256: CONTENT_SHA256 | None = None
    source_url: str | None = Field(default=None, max_length=4096)
    note: str | None = Field(default=None, max_length=8192)
    creator_hint: str | None = Field(default=None, max_length=512)
    content_kind_hint: RegistryKey | None = None
    asset_id: str | None = None
    source_id: str | None = None
    project_id: str | None = None
    probe_state: PreparationState = PreparationState.PENDING
    thumbnail_state: PreparationState = PreparationState.PENDING
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=4096)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime) -> datetime:
        checked = _aware(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_record(self):
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")
        if self.kind is IntakeKind.URL_NOTE and not (self.source_url or self.note):
            raise ValueError("URL/note intake requires source_url or note")
        if self.state in {IntakeState.PREPARED, IntakeState.PARTIAL}:
            if self.project_id is None:
                raise ValueError("prepared/partial intake requires project_id")
            if self.kind is IntakeKind.FILE and self.asset_id is None:
                raise ValueError("prepared/partial file intake requires asset_id")
        if self.state is IntakeState.FAILED and self.error_code is None:
            raise ValueError("failed intake requires error_code")
        return self


class PairingChallenge(FrozenModel):
    challenge_id: APP_ID
    code: str = Field(min_length=8, max_length=8, pattern=r"^[0-9]{8}$")
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime) -> datetime:
        checked = _aware(value)
        assert checked is not None
        return checked


class AuthSession(FrozenModel):
    session_id: APP_ID
    label: str | None = Field(default=None, max_length=256)
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_datetimes(cls, value: datetime) -> datetime:
        checked = _aware(value)
        assert checked is not None
        return checked
