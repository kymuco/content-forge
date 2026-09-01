from __future__ import annotations

import hashlib
import sys
import types
import wave
from pathlib import Path

import pytest

import content_forge.providers.qwen_tts as qwen_tts_module
from content_forge.providers import (
    QwenTTSConfig,
    QwenTTSProvider,
    TTSGenerationSettings,
    TTSRequest,
    TTSResponseError,
    TTSVoiceReference,
    semantic_tts_request_digest,
    tts_cache_key,
)

_TEST_REVISION = "1" * 40
_OTHER_REVISION = "2" * 40


class _CustomRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_custom_voice(self, **kwargs):
        self.calls.append(kwargs)
        return ([[0.0, 0.25, -0.25, 1.0, -1.0]], 24000)


class _CloneRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_voice_clone(self, **kwargs):
        self.calls.append(kwargs)
        return ([[0.0, 0.1, -0.1]], 16000)


class _DesignRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_voice_design(self, **kwargs):
        self.calls.append(kwargs)
        return ([[0.0, 0.2]], 22050)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_tts_semantic_identity_excludes_local_paths_and_tracks_generation(tmp_path: Path) -> None:
    ref_a = tmp_path / "ref-a.wav"
    ref_b = tmp_path / "elsewhere" / "ref-b.wav"
    ref_a.write_bytes(b"same-reference")
    ref_b.parent.mkdir()
    ref_b.write_bytes(b"same-reference")
    digest = _sha(ref_a)

    first = TTSRequest(
        output_path=tmp_path / "one.wav",
        text="Hello",
        language="en-US",
        voice_id="Ryan",
        instruction="Calm",
        reference=TTSVoiceReference(
            audio_path=ref_a,
            audio_sha256=digest,
            text="Reference",
        ),
        generation=TTSGenerationSettings(top_p=0.9, max_new_tokens=1000),
    )
    second = first.validated_copy(
        update={
            "output_path": tmp_path / "two.wav",
            "reference": first.reference.validated_copy(update={"audio_path": ref_b}),
        }
    )
    assert semantic_tts_request_digest(first) == semantic_tts_request_digest(second)

    changed = second.validated_copy(
        update={"generation": TTSGenerationSettings(top_p=0.8, max_new_tokens=1000)}
    )
    assert semantic_tts_request_digest(changed) != semantic_tts_request_digest(first)


def test_qwen_resolves_complete_repository_at_exact_commit(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    fake_hub = types.ModuleType("huggingface_hub")

    def snapshot_download(*, repo_id: str, revision: str) -> str:
        calls.append((repo_id, revision))
        return str(tmp_path)

    fake_hub.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    config = QwenTTSConfig(revision=_TEST_REVISION)
    assert qwen_tts_module._resolve_model_snapshot(config) == tmp_path
    assert calls == [(config.model_id, _TEST_REVISION)]

    with pytest.raises(ValueError):
        QwenTTSConfig(revision="main")


def test_qwen_custom_voice_is_lazy_and_writes_verified_pcm16(tmp_path: Path) -> None:
    runtime = _CustomRuntime()
    factory_calls = 0

    def factory(_config):
        nonlocal factory_calls
        factory_calls += 1
        return runtime

    provider = QwenTTSProvider(
        QwenTTSConfig(
            model_id="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
            mode="custom_voice",
        ),
        runtime_factory=factory,
        provider_version="0.1.1",
    )
    health = provider.health()
    assert health.available is True
    assert health.model_revision == "85e237c12c027371202489a0ec509ded67b5e4b5"
    assert factory_calls == 0

    request = TTSRequest(
        output_path=tmp_path / "custom.wav",
        text="Hello there",
        language="en-US",
        voice_id="Ryan",
        generation=TTSGenerationSettings(top_p=0.91, max_new_tokens=512),
    )
    result = provider.synthesize(request)
    assert factory_calls == 1
    assert runtime.calls == [
        {
            "text": "Hello there",
            "language": "English",
            "speaker": "Ryan",
            "max_new_tokens": 512,
            "top_p": 0.91,
        }
    ]
    assert result.evidence.request_sha256 == semantic_tts_request_digest(request)
    assert result.evidence.model_revision == health.model_revision
    assert result.audio_sha256 == _sha(request.output_path)
    assert result.sample_count == 5
    with wave.open(str(request.output_path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 24000
        assert handle.getnframes() == 5

    assert tts_cache_key(request, health) != tts_cache_key(
        request.validated_copy(update={"voice_id": "Aiden"}),
        health,
    )
    different_revision = QwenTTSProvider(
        QwenTTSConfig(revision=_OTHER_REVISION),
        runtime_factory=lambda _config: runtime,
        provider_version="0.1.1",
    ).health()
    assert different_revision.model_id == health.model_id
    assert different_revision.model_revision != health.model_revision
    assert tts_cache_key(request, different_revision) != tts_cache_key(request, health)


def test_qwen_06b_rejects_instruction_while_17b_forwards_it(tmp_path: Path) -> None:
    default_runtime = _CustomRuntime()
    default_provider = QwenTTSProvider(
        runtime_factory=lambda _config: default_runtime,
        provider_version="0.1.1",
    )
    with pytest.raises(TTSResponseError, match="0.6B CustomVoice ignores instructions"):
        default_provider.synthesize(
            TTSRequest(
                output_path=tmp_path / "ignored.wav",
                text="Do not silently ignore this",
                language="en",
                voice_id="Ryan",
                instruction="Very calm",
            )
        )
    assert default_runtime.calls == []

    runtime = _CustomRuntime()
    provider = QwenTTSProvider(
        QwenTTSConfig(
            model_id="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            revision=_TEST_REVISION,
            mode="custom_voice",
        ),
        runtime_factory=lambda _config: runtime,
        provider_version="0.1.1",
    )
    request = TTSRequest(
        output_path=tmp_path / "instruct.wav",
        text="Instruction-aware line",
        language="en",
        voice_id="Ryan",
        instruction="Very calm",
    )
    provider.synthesize(request)
    assert runtime.calls == [
        {
            "text": "Instruction-aware line",
            "language": "English",
            "speaker": "Ryan",
            "instruct": "Very calm",
        }
    ]


def test_qwen_voice_clone_verifies_reference_and_maps_arguments(tmp_path: Path) -> None:
    reference = tmp_path / "reference.bin"
    reference.write_bytes(b"voice-reference")
    runtime = _CloneRuntime()
    provider = QwenTTSProvider(
        QwenTTSConfig(
            model_id="Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            revision=_TEST_REVISION,
            mode="voice_clone",
        ),
        runtime_factory=lambda _config: runtime,
        provider_version="0.1.1",
    )
    request = TTSRequest(
        output_path=tmp_path / "clone.wav",
        text="New sentence",
        language="ja",
        voice_id="project-clone-a",
        reference=TTSVoiceReference(
            audio_path=reference,
            audio_sha256=_sha(reference),
            text="Reference transcript",
        ),
    )
    result = provider.synthesize(request)
    assert result.evidence.model_revision == _TEST_REVISION
    assert runtime.calls == [
        {
            "text": "New sentence",
            "language": "Japanese",
            "ref_audio": str(reference),
            "x_vector_only_mode": False,
            "ref_text": "Reference transcript",
        }
    ]

    reference.write_bytes(b"tampered")
    with pytest.raises(TTSResponseError, match="digest mismatch"):
        provider.synthesize(request.validated_copy(update={"output_path": tmp_path / "bad.wav"}))


def test_qwen_voice_design_requires_instruction_and_auto_language(tmp_path: Path) -> None:
    runtime = _DesignRuntime()
    provider = QwenTTSProvider(
        QwenTTSConfig(
            model_id="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            revision=_TEST_REVISION,
            mode="voice_design",
        ),
        runtime_factory=lambda _config: runtime,
        provider_version="0.1.1",
    )
    request = TTSRequest(
        output_path=tmp_path / "design.wav",
        text="Designed voice",
        voice_id="design-a",
        instruction="A warm restrained narrator",
    )
    result = provider.synthesize(request)
    assert result.evidence.model_revision == _TEST_REVISION
    assert runtime.calls == [
        {
            "text": "Designed voice",
            "language": "Auto",
            "instruct": "A warm restrained narrator",
        }
    ]

    with pytest.raises(TTSResponseError, match="requires an instruction"):
        provider.synthesize(
            request.validated_copy(
                update={"output_path": tmp_path / "missing.wav", "instruction": None}
            )
        )


def test_qwen_rejects_mode_mismatch_unpinned_model_unsupported_language_and_multiwave(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="does not match mode"):
        QwenTTSConfig(
            model_id="Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            mode="custom_voice",
        )
    with pytest.raises(ValueError, match="explicit model revision"):
        QwenTTSConfig(
            model_id="Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            mode="voice_clone",
        )

    runtime = _CustomRuntime()
    provider = QwenTTSProvider(
        runtime_factory=lambda _config: runtime,
        provider_version="0.1.1",
    )
    with pytest.raises(TTSResponseError, match="does not support language"):
        provider.synthesize(
            TTSRequest(
                output_path=tmp_path / "unsupported.wav",
                text="test",
                language="ar",
                voice_id="Ryan",
            )
        )

    class MultiRuntime:
        def generate_custom_voice(self, **_kwargs):
            return ([[0.0]], [[0.0]], 24000)

    bad = QwenTTSProvider(
        runtime_factory=lambda _config: MultiRuntime(),
        provider_version="0.1.1",
    )
    with pytest.raises(Exception):
        bad.synthesize(
            TTSRequest(
                output_path=tmp_path / "bad.wav",
                text="test",
                language="en",
                voice_id="Ryan",
            )
        )
