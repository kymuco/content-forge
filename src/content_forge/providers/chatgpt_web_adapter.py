"""Optional PR15 LLMProvider backed by chatgpt-web-adapter's production runtime."""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Callable

from .llm import (
    ClassificationRequest,
    ClassificationResult,
    HookRequest,
    HookSuggestions,
    LLMExecutionError,
    LLMInvocationEvidence,
    LLMProviderHealth,
    LLMResponseError,
    LLMUnavailableError,
    MetadataCandidate,
    MetadataRequest,
    MetadataSuggestions,
    TextCleanupRequest,
    TextCleanupResult,
    TranslationRequest,
    TranslationResult,
    build_task_prompt,
    response_digest,
    semantic_request_digest,
    strict_json_object,
)

_PROVIDER_ID = "chatgpt_web_adapter"
_SUPPORTED_MODEL_PROFILES = frozenset({"FAST", "BALANCED", "DEEP"})
_SUPPORTED_CONVERSATION_MODES = frozenset({"normal", "temporary"})


def _package_version() -> str:
    try:
        return importlib_metadata.version("chatgpt-web-adapter")
    except importlib_metadata.PackageNotFoundError:
        return "unavailable"


def _enum_text(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else str(raw)


def _exact_object(
    payload: dict[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(payload)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise LLMResponseError("LLM JSON is missing required fields: " + ", ".join(sorted(missing)))
    if unknown:
        raise LLMResponseError("LLM JSON contains unknown fields: " + ", ".join(sorted(unknown)))


def _string_list(value: object, *, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LLMResponseError(f"LLM field {field} must be a non-empty JSON array")
    if len(value) > maximum:
        raise LLMResponseError(f"LLM field {field} exceeds requested maximum")
    if any(not isinstance(item, str) or not item for item in value):
        raise LLMResponseError(f"LLM field {field} must contain non-empty strings")
    values = tuple(value)
    if len(set(values)) != len(values):
        raise LLMResponseError(f"LLM field {field} must not contain duplicates")
    return values


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LLMResponseError(f"LLM field {field} must be a string or null")
    return value


class ChatGPTWebAdapterLLMProvider:
    """Task-oriented adapter around CWA 0.2 ChatGPTProductRuntime.

    The dependency is imported lazily. No live call is attempted by package import, tests,
    timeline compilation, or rendering. Each task uses a new conversation by default;
    Temporary Chat is selected by default to avoid accumulating provider task history.
    """

    def __init__(
        self,
        *,
        auth_file: str | Path = "auth_data.json",
        transport: str = "browser-owned",
        model_profile: str = "BALANCED",
        conversation_mode: str = "temporary",
        timeout: float = 150.0,
        poll_interval: float = 0.5,
        runtime_factory: Callable[..., object] | None = None,
    ) -> None:
        profile = model_profile.strip().upper()
        if profile not in _SUPPORTED_MODEL_PROFILES:
            raise ValueError("model_profile must be FAST, BALANCED, or DEEP")
        mode = conversation_mode.strip().lower()
        if mode not in _SUPPORTED_CONVERSATION_MODES:
            raise ValueError("conversation_mode must be normal or temporary")
        if timeout <= 0.0:
            raise ValueError("timeout must be positive")
        if poll_interval <= 0.0:
            raise ValueError("poll_interval must be positive")
        if not transport.strip():
            raise ValueError("transport must be non-empty")

        self.auth_file = Path(auth_file)
        self.transport = transport.strip()
        self.model_profile = profile
        self.conversation_mode = mode
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._runtime_factory = runtime_factory
        self._runtime: object | None = None
        self._provider_version = (
            "injected-test-runtime" if runtime_factory is not None else _package_version()
        )

    @property
    def provider_id(self) -> str:
        return _PROVIDER_ID

    @property
    def provider_version(self) -> str:
        return self._provider_version

    def _assemble_runtime(self) -> object:
        factory = self._runtime_factory
        if factory is None:
            try:
                from chatgpt_web_adapter import assemble_product_runtime
            except ImportError as exc:
                raise LLMUnavailableError(
                    "chatgpt-web-adapter is not installed; install content-forge[llm]"
                ) from exc
            factory = assemble_product_runtime
        try:
            return factory(transport=self.transport, auth_file=self.auth_file)
        except Exception as exc:
            raise LLMUnavailableError("failed to assemble chatgpt-web-adapter runtime") from exc

    def _get_runtime(self) -> object:
        if self._runtime is None:
            self._runtime = self._assemble_runtime()
        return self._runtime

    def health(self) -> LLMProviderHealth:
        try:
            runtime = self._get_runtime()
            health = runtime.health()  # type: ignore[attr-defined]
        except LLMUnavailableError as exc:
            return LLMProviderHealth(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                available=False,
                reason=str(exc),
            )
        except Exception as exc:
            return LLMProviderHealth(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                available=False,
                reason=f"chatgpt-web-adapter health check failed: {type(exc).__name__}",
            )
        ready = getattr(health, "ready", None)
        if ready is not True:
            reason = getattr(health, "reason", None)
            return LLMProviderHealth(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                available=False,
                reason=(str(reason) if reason else "chatgpt-web-adapter runtime is not ready"),
            )
        return LLMProviderHealth(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            available=True,
        )

    def _evidence(
        self,
        *,
        task: str,
        request: object,
        raw_response: str,
        execution: object,
    ) -> LLMInvocationEvidence:
        response = getattr(execution, "response", None)
        request_meta = getattr(response, "request", None)
        observed_model = getattr(request_meta, "observed_model", None)
        provenance = getattr(execution, "provenance", None)
        completion = getattr(provenance, "completion", None)
        canonical_completion = getattr(completion, "canonical_completion_proven", None)
        completion_source = _enum_text(getattr(completion, "source", None))
        return LLMInvocationEvidence(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            transport=str(getattr(execution, "transport", self.transport)),
            model_profile=self.model_profile,
            observed_model=(
                observed_model if isinstance(observed_model, str) and observed_model else None
            ),
            request_sha256=semantic_request_digest(task, request),  # type: ignore[arg-type]
            response_sha256=response_digest(raw_response),
            canonical_completion_proven=(
                canonical_completion if isinstance(canonical_completion, bool) else None
            ),
            completion_source=completion_source,
        )

    def _invoke(self, task: str, request: object) -> tuple[dict[str, object], LLMInvocationEvidence]:
        availability = self.health()
        if not availability.available:
            raise LLMUnavailableError(availability.reason or "LLM provider is unavailable")
        runtime = self._get_runtime()
        prompt = build_task_prompt(task, request)  # type: ignore[arg-type]
        try:
            execution = runtime.send_text_observed(  # type: ignore[attr-defined]
                prompt,
                timeout=self.timeout,
                poll_interval=self.poll_interval,
                conversation_mode=self.conversation_mode,
                model_profile=self.model_profile,
            )
        except Exception as exc:
            # No automatic retry: CWA production writes can require reconciliation when
            # dispatch outcome is ambiguous, so PR15 never duplicates a provider turn.
            raise LLMExecutionError("chatgpt-web-adapter task execution failed") from exc

        response = getattr(execution, "response", None)
        raw_text = getattr(response, "text", None)
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise LLMResponseError("chatgpt-web-adapter returned no assistant text")
        payload = strict_json_object(raw_text)
        evidence = self._evidence(
            task=task,
            request=request,
            raw_response=raw_text,
            execution=execution,
        )
        return payload, evidence

    def suggest_hooks(self, request: HookRequest) -> HookSuggestions:
        payload, evidence = self._invoke("hook_suggestions", request)
        _exact_object(payload, required=frozenset({"hooks"}))
        hooks = _string_list(payload["hooks"], field="hooks", maximum=request.max_candidates)
        return HookSuggestions(hooks=hooks, evidence=evidence)

    def suggest_metadata(self, request: MetadataRequest) -> MetadataSuggestions:
        payload, evidence = self._invoke("metadata_suggestions", request)
        _exact_object(payload, required=frozenset({"candidates"}))
        raw_candidates = payload["candidates"]
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise LLMResponseError("metadata candidates must be a non-empty JSON array")
        if len(raw_candidates) > request.max_candidates:
            raise LLMResponseError("metadata candidates exceed requested maximum")
        candidates: list[MetadataCandidate] = []
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                raise LLMResponseError("each metadata candidate must be a JSON object")
            _exact_object(
                raw,
                required=frozenset({"title", "description", "hashtags"}),
            )
            hashtags = raw["hashtags"]
            if not isinstance(hashtags, list):
                raise LLMResponseError("metadata hashtags must be a JSON array")
            if len(hashtags) > request.max_hashtags:
                raise LLMResponseError("metadata hashtags exceed requested maximum")
            if any(not isinstance(item, str) or not item for item in hashtags):
                raise LLMResponseError("metadata hashtags must contain non-empty strings")
            try:
                candidate = MetadataCandidate(
                    title=raw["title"],
                    description=raw["description"],
                    hashtags=tuple(hashtags),
                )
            except Exception as exc:
                raise LLMResponseError("invalid metadata candidate") from exc
            candidates.append(candidate)
        return MetadataSuggestions(candidates=tuple(candidates), evidence=evidence)

    def clean_text(self, request: TextCleanupRequest) -> TextCleanupResult:
        payload, evidence = self._invoke("text_cleanup", request)
        _exact_object(
            payload,
            required=frozenset({"cleaned_text"}),
            optional=frozenset({"notes"}),
        )
        cleaned = payload["cleaned_text"]
        if not isinstance(cleaned, str) or not cleaned:
            raise LLMResponseError("cleaned_text must be a non-empty string")
        notes = _optional_string(payload.get("notes"), field="notes")
        try:
            return TextCleanupResult(cleaned_text=cleaned, notes=notes, evidence=evidence)
        except Exception as exc:
            raise LLMResponseError("invalid text cleanup result") from exc

    def translate(self, request: TranslationRequest) -> TranslationResult:
        payload, evidence = self._invoke("translation", request)
        _exact_object(
            payload,
            required=frozenset({"translated_text"}),
            optional=frozenset({"notes"}),
        )
        translated = payload["translated_text"]
        if not isinstance(translated, str) or not translated:
            raise LLMResponseError("translated_text must be a non-empty string")
        notes = _optional_string(payload.get("notes"), field="notes")
        try:
            return TranslationResult(
                translated_text=translated,
                notes=notes,
                evidence=evidence,
            )
        except Exception as exc:
            raise LLMResponseError("invalid translation result") from exc

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        payload, evidence = self._invoke("classification", request)
        _exact_object(
            payload,
            required=frozenset({"content_kinds", "template_ids", "tags"}),
            optional=frozenset({"rationale"}),
        )
        content_kinds = _string_list(payload["content_kinds"], field="content_kinds", maximum=8)
        template_ids = _string_list(payload["template_ids"], field="template_ids", maximum=12)
        tags_value = payload["tags"]
        if not isinstance(tags_value, list):
            raise LLMResponseError("classification tags must be a JSON array")
        if len(tags_value) > request.max_tags:
            raise LLMResponseError("classification tags exceed requested maximum")
        if any(not isinstance(item, str) or not item for item in tags_value):
            raise LLMResponseError("classification tags must contain non-empty strings")
        tags = tuple(tags_value)
        if len(set(tags)) != len(tags):
            raise LLMResponseError("classification tags must not contain duplicates")
        disallowed_kinds = set(content_kinds) - set(request.allowed_content_kinds)
        disallowed_templates = set(template_ids) - set(request.allowed_template_ids)
        if disallowed_kinds:
            raise LLMResponseError(
                "classification returned content kind outside allowlist: "
                + ", ".join(sorted(disallowed_kinds))
            )
        if disallowed_templates:
            raise LLMResponseError(
                "classification returned template outside allowlist: "
                + ", ".join(sorted(disallowed_templates))
            )
        rationale = _optional_string(payload.get("rationale"), field="rationale")
        try:
            return ClassificationResult(
                content_kinds=content_kinds,
                template_ids=template_ids,
                tags=tags,
                rationale=rationale,
                evidence=evidence,
            )
        except Exception as exc:
            raise LLMResponseError("invalid classification result") from exc


__all__ = ["ChatGPTWebAdapterLLMProvider"]
