from __future__ import annotations

from types import SimpleNamespace

import pytest

from content_forge.providers import (
    ChatGPTWebAdapterLLMProvider,
    ClassificationRequest,
    HookRequest,
    LLMProvider,
    LLMResponseError,
    LLMUnavailableError,
    MetadataRequest,
    TextCleanupRequest,
    TranslationRequest,
)


class _FakeRuntime:
    def __init__(self, replies: list[str], *, ready: bool = True) -> None:
        self.replies = list(replies)
        self.ready = ready
        self.calls: list[tuple[str, dict[str, object]]] = []

    def health(self):
        return SimpleNamespace(ready=self.ready, reason=None if self.ready else "offline")

    def send_text_observed(self, prompt: str, **kwargs):
        self.calls.append((prompt, kwargs))
        raw = self.replies.pop(0)
        return SimpleNamespace(
            transport="browser-owned",
            response=SimpleNamespace(
                text=raw,
                request=SimpleNamespace(observed_model="synthetic-model"),
            ),
            provenance=SimpleNamespace(
                completion=SimpleNamespace(
                    canonical_completion_proven=True,
                    source=SimpleNamespace(value="CANONICAL_READBACK"),
                )
            ),
        )


def _provider(runtime: _FakeRuntime) -> ChatGPTWebAdapterLLMProvider:
    assembled: list[dict[str, object]] = []

    def factory(**kwargs):
        assembled.append(kwargs)
        return runtime

    provider = ChatGPTWebAdapterLLMProvider(runtime_factory=factory)
    provider._assembled = assembled  # type: ignore[attr-defined]
    return provider


def test_adapter_implements_protocol_and_uses_temporary_balanced_runtime_defaults() -> None:
    runtime = _FakeRuntime(['{"hooks":["First","Second"]}'])
    provider = _provider(runtime)

    assert isinstance(provider, LLMProvider)
    result = provider.suggest_hooks(HookRequest(source_summary="fixture", max_candidates=2))

    assert result.hooks == ("First", "Second")
    assert result.evidence.provider_id == "chatgpt_web_adapter"
    assert result.evidence.observed_model == "synthetic-model"
    assert result.evidence.canonical_completion_proven is True
    assert result.evidence.completion_source == "CANONICAL_READBACK"
    assert runtime.calls[0][1]["conversation_mode"] == "temporary"
    assert runtime.calls[0][1]["model_profile"] == "BALANCED"
    assert provider._assembled == [  # type: ignore[attr-defined]
        {"transport": "browser-owned", "auth_file": provider.auth_file}
    ]


def test_adapter_supports_all_pr15_task_shapes_without_live_dependency() -> None:
    runtime = _FakeRuntime(
        [
            '{"candidates":[{"title":"T","description":"D","hashtags":["#x"]}]}',
            '{"cleaned_text":"clean","notes":null}',
            '{"translated_text":"hola","notes":"neutral"}',
            '{"content_kinds":["moment"],"template_ids":["hook_topbar"],"tags":["game"],"rationale":"fit"}',
        ]
    )
    provider = _provider(runtime)

    metadata = provider.suggest_metadata(MetadataRequest(source_summary="fixture"))
    cleanup = provider.clean_text(TextCleanupRequest(raw_text="clen"))
    translation = provider.translate(
        TranslationRequest(text="hello", source_language="en", target_language="es")
    )
    classification = provider.classify(
        ClassificationRequest(
            source_summary="fixture",
            allowed_content_kinds=("moment", "art_story"),
            allowed_template_ids=("hook_topbar", "art_story"),
        )
    )

    assert metadata.candidates[0].title == "T"
    assert cleanup.cleaned_text == "clean"
    assert translation.translated_text == "hola"
    assert classification.content_kinds == ("moment",)
    assert classification.template_ids == ("hook_topbar",)


def test_adapter_fails_closed_on_unknown_fields_trailing_prose_and_allowlist_escape() -> None:
    runtime = _FakeRuntime(
        [
            '{"hooks":["A"],"accepted":true}',
            '{"hooks":["A"]} trailing',
            '{"content_kinds":["invented"],"template_ids":["hook_topbar"],"tags":[]}',
        ]
    )
    provider = _provider(runtime)

    with pytest.raises(LLMResponseError, match="unknown fields"):
        provider.suggest_hooks(HookRequest(source_summary="fixture"))
    with pytest.raises(LLMResponseError):
        provider.suggest_hooks(HookRequest(source_summary="fixture"))
    with pytest.raises(LLMResponseError, match="outside allowlist"):
        provider.classify(
            ClassificationRequest(
                source_summary="fixture",
                allowed_content_kinds=("moment",),
                allowed_template_ids=("hook_topbar",),
            )
        )


def test_adapter_unavailable_health_degrades_feature_without_project_or_renderer_dependency() -> None:
    runtime = _FakeRuntime([], ready=False)
    provider = _provider(runtime)

    health = provider.health()
    assert health.available is False
    assert health.reason == "offline"
    with pytest.raises(LLMUnavailableError, match="offline"):
        provider.suggest_hooks(HookRequest(source_summary="fixture"))
