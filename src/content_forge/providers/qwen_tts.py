"""Optional local Qwen3-TTS adapter for the PR20 TTS provider contract."""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
import struct
import wave
from collections.abc import Callable, Iterable
from numbers import Integral
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from content_forge.core.models import FrozenModel

from .tts import (
    TTSExecutionError,
    TTSInvocationEvidence,
    TTSProviderHealth,
    TTSRequest,
    TTSResponseError,
    TTSResult,
    TTSUnavailableError,
    semantic_tts_request_digest,
    tts_config_digest,
)

_PROVIDER_ID = "qwen3_tts_local"
_SUPPORTED_QWEN_TTS_SERIES = (0, 1)
_DEFAULT_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
_DEFAULT_MODEL_REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
_LANGUAGE_NAMES = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
}


class QwenTTSConfig(FrozenModel):
    """Portable Qwen3-TTS model intent; weights/device remain local runtime concerns."""

    model_id: str = Field(default=_DEFAULT_MODEL_ID, min_length=1, max_length=512)
    revision: str = Field(default=_DEFAULT_MODEL_REVISION, min_length=1, max_length=256)
    mode: Literal["custom_voice", "voice_clone", "voice_design"] = "custom_voice"
    device_map: str = Field(default="auto", min_length=1, max_length=128)
    dtype: Literal["bfloat16", "float16", "float32"] = "bfloat16"
    attn_implementation: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def model_matches_mode_and_revision(self):
        folded = self.model_id.casefold()
        markers = {
            "custom_voice": "customvoice",
            "voice_clone": "base",
            "voice_design": "voicedesign",
        }
        marker = markers[self.mode]
        if marker not in folded:
            raise ValueError(f"Qwen TTS model_id does not match mode {self.mode!r}")
        if self.model_id != _DEFAULT_MODEL_ID and self.revision == _DEFAULT_MODEL_REVISION:
            raise ValueError("non-default Qwen TTS model requires an explicit model revision")
        return self


RuntimeFactory = Callable[[QwenTTSConfig], Any]


def _installed_version() -> str:
    try:
        version = importlib.metadata.version("qwen-tts")
    except importlib.metadata.PackageNotFoundError as exc:
        raise TTSUnavailableError(
            "qwen-tts is not installed; install the optional local PR20 TTS runtime"
        ) from exc
    parts = version.split(".")
    try:
        series = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError) as exc:
        raise TTSUnavailableError(f"unrecognized qwen-tts version {version!r}") from exc
    if series != _SUPPORTED_QWEN_TTS_SERIES:
        raise TTSUnavailableError(
            f"unsupported qwen-tts version {version!r}; PR20 expects 0.1.x"
        )
    return version


def _default_runtime_factory(config: QwenTTSConfig):
    _installed_version()
    try:
        import torch
        from qwen_tts import Qwen3TTSModel
    except Exception as exc:  # pragma: no cover - optional environment-specific dependency
        raise TTSUnavailableError("Qwen3-TTS import failed") from exc

    dtype = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }[config.dtype]
    kwargs: dict[str, object] = {
        "revision": config.revision,
        "device_map": config.device_map,
        "dtype": dtype,
    }
    if config.attn_implementation is not None:
        kwargs["attn_implementation"] = config.attn_implementation
    try:
        return Qwen3TTSModel.from_pretrained(config.model_id, **kwargs)
    except Exception as exc:  # pragma: no cover - weights/GPU availability is local
        raise TTSUnavailableError("Qwen3-TTS model initialization failed") from exc


def _language_name(value: str | None) -> str:
    if value is None or value.casefold() == "und":
        return "Auto"
    base = value.split("-", 1)[0].casefold()
    try:
        return _LANGUAGE_NAMES[base]
    except KeyError as exc:
        raise TTSResponseError(f"Qwen3-TTS does not support language {value!r}") from exc


def _generation_kwargs(request: TTSRequest) -> dict[str, object]:
    return request.generation.model_dump(mode="python", exclude_none=True)


def _verify_reference(request: TTSRequest) -> None:
    reference = request.reference
    if reference is None:
        return
    path = Path(reference.audio_path)
    if not path.is_file():
        raise TTSResponseError("Qwen3-TTS reference audio is missing")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != reference.audio_sha256:
        raise TTSResponseError("Qwen3-TTS reference audio digest mismatch")


def _one_waveform(value: object) -> object:
    if isinstance(value, (str, bytes, bytearray)):
        raise TTSResponseError("Qwen3-TTS waveform result is malformed")
    try:
        items = list(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TTSResponseError("Qwen3-TTS waveform result is not iterable") from exc
    if len(items) != 1:
        raise TTSResponseError("PR20 line synthesis expects exactly one Qwen3-TTS waveform")
    return items[0]


def _finite_samples(waveform: object) -> tuple[float, ...]:
    tolist = getattr(waveform, "tolist", None)
    if callable(tolist):
        waveform = tolist()
    if isinstance(waveform, (str, bytes, bytearray)):
        raise TTSResponseError("Qwen3-TTS waveform is malformed")
    try:
        raw: Iterable[object] = waveform  # type: ignore[assignment]
        samples = tuple(float(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise TTSResponseError("Qwen3-TTS waveform must be one-dimensional numeric audio") from exc
    if not samples:
        raise TTSResponseError("Qwen3-TTS returned an empty waveform")
    if any(not math.isfinite(item) for item in samples):
        raise TTSResponseError("Qwen3-TTS waveform contains non-finite samples")
    return samples


def _pcm16_sample(value: float) -> int:
    if value <= -1.0:
        return -32768
    if value >= 1.0:
        return 32767
    return int(round(value * 32767.0))


def _write_pcm16_wav(path: Path, samples: tuple[float, ...], sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for sample in samples:
        frames.extend(struct.pack("<h", _pcm16_sample(sample)))
    try:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(bytes(frames))
    except (wave.Error, OSError) as exc:
        raise TTSExecutionError("failed to encode Qwen3-TTS waveform as PCM16 WAV") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class QwenTTSProvider:
    """Lazy Qwen3-TTS 0.1.x adapter with explicit model-mode boundaries."""

    def __init__(
        self,
        config: QwenTTSConfig | None = None,
        *,
        runtime_factory: RuntimeFactory | None = None,
        provider_version: str | None = None,
    ) -> None:
        self.config = config or QwenTTSConfig()
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._runtime = None
        self._provider_version_override = provider_version

    def _provider_version(self) -> str:
        return self._provider_version_override or _installed_version()

    def _get_runtime(self):
        if self._runtime is None:
            self._runtime = self._runtime_factory(self.config)
        return self._runtime

    def _config_sha256(self) -> str:
        return tts_config_digest(self.config.model_dump(mode="json"))

    def health(self) -> TTSProviderHealth:
        """Report package/config identity without loading multi-gigabyte model weights."""

        try:
            version = self._provider_version()
        except TTSUnavailableError as exc:
            return TTSProviderHealth(
                provider_id=_PROVIDER_ID,
                provider_version=self._provider_version_override or "unavailable",
                model_id=self.config.model_id,
                model_revision=self.config.revision,
                config_sha256=self._config_sha256(),
                available=False,
                reason=str(exc),
            )
        return TTSProviderHealth(
            provider_id=_PROVIDER_ID,
            provider_version=version,
            model_id=self.config.model_id,
            model_revision=self.config.revision,
            config_sha256=self._config_sha256(),
            available=True,
        )

    def synthesize(self, request: TTSRequest) -> TTSResult:
        language = _language_name(request.language)
        _verify_reference(request)
        if self.config.mode == "voice_clone":
            if request.reference is None:
                raise TTSResponseError("Qwen3-TTS voice_clone requires reference audio")
            if not request.reference.x_vector_only_mode and request.reference.text is None:
                raise TTSResponseError(
                    "Qwen3-TTS voice_clone requires reference text unless x_vector_only_mode is enabled"
                )
        elif request.reference is not None:
            raise TTSResponseError(
                f"Qwen3-TTS {self.config.mode} does not accept reference audio"
            )
        if self.config.mode == "voice_design" and request.instruction is None:
            raise TTSResponseError("Qwen3-TTS voice_design requires an instruction")

        try:
            runtime = self._get_runtime()
            kwargs = _generation_kwargs(request)
            if self.config.mode == "custom_voice":
                call = {
                    "text": request.text,
                    "language": language,
                    "speaker": request.voice_id,
                    **kwargs,
                }
                if request.instruction is not None:
                    call["instruct"] = request.instruction
                waveforms, sample_rate = runtime.generate_custom_voice(**call)
            elif self.config.mode == "voice_clone":
                assert request.reference is not None
                call = {
                    "text": request.text,
                    "language": language,
                    "ref_audio": str(request.reference.audio_path),
                    "x_vector_only_mode": request.reference.x_vector_only_mode,
                    **kwargs,
                }
                if request.reference.text is not None:
                    call["ref_text"] = request.reference.text
                waveforms, sample_rate = runtime.generate_voice_clone(**call)
            else:
                waveforms, sample_rate = runtime.generate_voice_design(
                    text=request.text,
                    language=language,
                    instruct=request.instruction,
                    **kwargs,
                )
        except TTSUnavailableError:
            raise
        except TTSResponseError:
            raise
        except Exception as exc:
            raise TTSExecutionError("Qwen3-TTS generation failed") from exc

        if isinstance(sample_rate, bool) or not isinstance(sample_rate, Integral):
            raise TTSResponseError("Qwen3-TTS returned an invalid sample rate")
        sample_rate = int(sample_rate)
        if sample_rate <= 0:
            raise TTSResponseError("Qwen3-TTS returned an invalid sample rate")
        samples = _finite_samples(_one_waveform(waveforms))
        output = Path(request.output_path)
        _write_pcm16_wav(output, samples, sample_rate)
        try:
            size_bytes = output.stat().st_size
            audio_sha256 = _sha256_file(output)
        except OSError as exc:
            raise TTSExecutionError("Qwen3-TTS output WAV could not be read back") from exc

        request_sha256 = semantic_tts_request_digest(request)
        return TTSResult(
            audio_sha256=audio_sha256,
            size_bytes=size_bytes,
            sample_rate_hz=sample_rate,
            channels=1,
            sample_count=len(samples),
            duration_seconds=len(samples) / sample_rate,
            evidence=TTSInvocationEvidence(
                provider_id=_PROVIDER_ID,
                provider_version=self._provider_version(),
                model_id=self.config.model_id,
                model_revision=self.config.revision,
                engine="qwen-tts",
                request_sha256=request_sha256,
                config_sha256=self._config_sha256(),
                resolved_voice=request.voice_id,
                resolved_language=language,
            ),
        )


__all__ = ["QwenTTSConfig", "QwenTTSProvider"]
