"""PR20 local TTS provider contracts and deterministic line-synthesis identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from content_forge.core.models import FrozenModel, LanguageTag, SHA256

_TTS_CONTRACT_VERSION = "pr20_tts_contract_v1"


class TTSProviderError(RuntimeError):
    """Base class for optional TTS provider failures."""


class TTSUnavailableError(TTSProviderError):
    """The optional TTS package/model/runtime is unavailable."""


class TTSExecutionError(TTSProviderError):
    """TTS execution failed before a validated Content Forge result existed."""


class TTSResponseError(TTSProviderError):
    """Provider output was malformed or violated the PR20 contract."""


class TTSProviderHealth(FrozenModel):
    """Stable provider identity available before an expensive synthesis call."""

    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=512)
    model_revision: str | None = Field(default=None, min_length=1, max_length=256)
    config_sha256: SHA256
    available: bool
    reason: str | None = Field(default=None, max_length=4096)


class TTSGenerationSettings(FrozenModel):
    """Portable generation knobs that participate in semantic cache identity."""

    max_new_tokens: int | None = Field(default=None, ge=1, le=65536)
    do_sample: bool | None = None
    temperature: float | None = Field(default=None, gt=0.0, le=10.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1, le=100000)
    repetition_penalty: float | None = Field(default=None, gt=0.0, le=100.0)


class TTSVoiceReference(FrozenModel):
    """Runtime reference-audio input whose local path is not semantic identity."""

    audio_path: Path
    audio_sha256: SHA256
    text: str | None = Field(default=None, max_length=30000)
    x_vector_only_mode: bool = False


class TTSRequest(FrozenModel):
    """One line synthesis request. Output and reference paths are machine-local."""

    output_path: Path
    text: str = Field(min_length=1, max_length=30000)
    language: LanguageTag | None = None
    voice_id: str = Field(min_length=1, max_length=256)
    instruction: str | None = Field(default=None, max_length=4096)
    reference: TTSVoiceReference | None = None
    generation: TTSGenerationSettings = Field(default_factory=TTSGenerationSettings)

    @model_validator(mode="after")
    def reject_blank_semantic_strings(self):
        if not self.text.strip():
            raise ValueError("TTS text must contain non-whitespace content")
        if not self.voice_id.strip():
            raise ValueError("TTS voice_id must contain non-whitespace content")
        if self.instruction is not None and not self.instruction.strip():
            raise ValueError("TTS instruction must be omitted instead of blank")
        if self.reference is not None and self.reference.text is not None:
            if not self.reference.text.strip():
                raise ValueError("TTS reference text must be omitted instead of blank")
        return self


class TTSInvocationEvidence(FrozenModel):
    contract_version: Literal["pr20_tts_contract_v1"] = _TTS_CONTRACT_VERSION
    provider_id: str = Field(min_length=1, max_length=128)
    provider_version: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=512)
    model_revision: str | None = Field(default=None, min_length=1, max_length=256)
    engine: str | None = Field(default=None, max_length=128)
    request_sha256: SHA256
    config_sha256: SHA256
    resolved_voice: str = Field(min_length=1, max_length=256)
    resolved_language: str | None = Field(default=None, max_length=64)


class TTSResult(FrozenModel):
    """Verified-description contract for one provider-produced PCM WAV artifact."""

    audio_sha256: SHA256
    size_bytes: int = Field(ge=1)
    sample_rate_hz: int = Field(ge=1, le=768000)
    channels: int = Field(ge=1, le=64)
    sample_count: int = Field(ge=1)
    duration_seconds: float = Field(gt=0.0)
    evidence: TTSInvocationEvidence


@runtime_checkable
class TTSProvider(Protocol):
    """Narrow local TTS interface; persistent character-to-voice casting is PR21."""

    def health(self) -> TTSProviderHealth: ...

    def synthesize(self, request: TTSRequest) -> TTSResult: ...


def _plain_generation(settings: TTSGenerationSettings) -> dict[str, object]:
    return settings.model_dump(mode="json", exclude_none=True)


def semantic_tts_request_digest(request: TTSRequest) -> str:
    """Hash semantic synthesis input while excluding machine-local filesystem paths."""

    reference = None
    if request.reference is not None:
        reference = {
            "audio_sha256": request.reference.audio_sha256,
            "text": request.reference.text,
            "x_vector_only_mode": request.reference.x_vector_only_mode,
        }
    payload = {
        "contract_version": _TTS_CONTRACT_VERSION,
        "text": request.text,
        "language": request.language,
        "voice_id": request.voice_id,
        "instruction": request.instruction,
        "reference": reference,
        "generation": _plain_generation(request.generation),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tts_config_digest(config: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tts_cache_key(request: TTSRequest, health: TTSProviderHealth) -> str:
    """Bind one reusable synthesis to semantic input and exact provider/model config."""

    payload = {
        "contract_version": _TTS_CONTRACT_VERSION,
        "request_sha256": semantic_tts_request_digest(request),
        "provider_id": health.provider_id,
        "provider_version": health.provider_version,
        "model_id": health.model_id,
        "model_revision": health.model_revision,
        "config_sha256": health.config_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "TTSExecutionError",
    "TTSGenerationSettings",
    "TTSInvocationEvidence",
    "TTSProvider",
    "TTSProviderError",
    "TTSProviderHealth",
    "TTSRequest",
    "TTSResponseError",
    "TTSResult",
    "TTSUnavailableError",
    "TTSVoiceReference",
    "semantic_tts_request_digest",
    "tts_cache_key",
    "tts_config_digest",
]
