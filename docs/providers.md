# Providers

## Purpose

Providers isolate capabilities that are useful but should remain replaceable, optional, or environment-specific.

Core project state, timelines, rendering, and storage must not depend directly on a particular LLM, OCR package, TTS model, source website, or publishing API.

The initial provider families are:

```text
LLMProvider
OCRProvider (later)
TTSProvider (later)
SourceProvider (later)
PublishingProvider (later)
AnalyticsProvider (later)
```

## General provider rules

Every provider integration should follow these rules where applicable:

1. **Optionality.** Disabling the provider should degrade a feature, not make unrelated rendering/storage fail.
2. **Attribution.** Generated suggestions/artifacts should retain provider/model/config metadata when useful for reproduction.
3. **Caching.** Repeatable expensive calls should be cached by semantic input.
4. **No silent canonical overwrite.** Suggested values never replace accepted human values without an explicit workflow rule.
5. **Structured failures.** Provider errors become task/job failures with retry/fallback behavior, not corrupted project files.
6. **Secrets stay local.** Cookies, tokens, credentials, and provider session data are runtime configuration and never repository content.
7. **Narrow interfaces.** Providers expose capabilities the application needs, not their entire underlying SDK/API.

## `LLMProvider`

The first real implementation is expected to use `chatgpt-web-adapter`.

The interface should be task-oriented rather than exposing arbitrary provider internals throughout the codebase.

Conceptual capabilities:

```python
class LLMProvider(Protocol):
    def suggest_hooks(self, request: HookRequest) -> HookSuggestions: ...
    def suggest_metadata(self, request: MetadataRequest) -> MetadataSuggestions: ...
    def clean_text(self, request: TextCleanupRequest) -> TextCleanupResult: ...
    def translate(self, request: TranslationRequest) -> TranslationResult: ...
    def classify(self, request: ClassificationRequest) -> ClassificationResult: ...
```

The exact Python contracts will be frozen in implementation PRs.

### Appropriate uses

#### Hook suggestions

Input can contain structured context such as:

```text
content kind
source/project note
game/character tags
short textual description
current hook
language
tone constraints
```

Output is several candidate hooks, not an automatically accepted headline by default.

#### Metadata suggestions

- title;
- description;
- hashtags/tags;
- CTA copy where a template uses it.

#### OCR cleanup

Later OCR providers return raw extracted text. The LLM can propose normalized punctuation/spelling while preserving raw OCR and requiring review when ambiguity matters.

#### Translation/localization

Translate accepted hooks/subtitles/metadata into project variants. The translation should be stored as variant data with provenance, not regenerated every render.

#### Classification/tag suggestions

The LLM can suggest:

- content kind;
- template candidates;
- game/character/topic tags;
- moment type.

These are convenience features. Asset identity and project validity do not depend on them.

### Inappropriate uses

Do not use the LLM to:

- decide whether a file exists;
- calculate deterministic scene timing when durations are known;
- generate FFmpeg command syntax when a compiler can do it deterministically;
- infer asset identity instead of hashing;
- own project state transitions;
- silently decide permissions/copyright status;
- replace exact source metadata with guesses.

## `chatgpt-web-adapter` integration

The implementation should be wrapped in a provider module so Content Forge depends on its own stable request/result models.

Conceptually:

```text
Content Forge task request
-> LLMProvider adapter
-> chatgpt-web-adapter
-> model response
-> parser/validation
-> Content Forge suggestion result
```

The adapter should handle:

- timeout/cancellation;
- malformed response parsing;
- unavailable session;
- provider-specific errors;
- request logging without leaking secrets;
- bounded retries where safe.

If the adapter is unavailable, manual fields and deterministic workflows remain usable.

## `OCRProvider` (later)

Conceptual result:

```text
regions[]:
  bbox
  raw_text
  confidence
  language (optional)
```

The provider should not be required to solve speaker attribution. OCR extracts text/regions; dialogue workflow decides reading order/speakers with human review as needed.

Candidate implementations can be benchmarked later rather than selected in the architecture phase.

Requirements:

- local execution preferred;
- preserve raw result;
- confidence available where provider supports it;
- cache by source hash + provider/model/config;
- correction stored separately from raw OCR.

## `TTSProvider` (later)

The intended first implementation is a local Qwen TTS integration, but core contracts should remain model-agnostic.

Conceptual request:

```text
text
voice_id
style/settings
language
seed/determinism options where supported
```

Conceptual result:

```text
audio asset
duration
provider/model/version
resolved voice/settings
```

### Per-line synthesis

Dialogue should be synthesized one line at a time rather than as one large scene file.

Benefits:

- cache reuse;
- isolated regeneration;
- known line durations;
- straightforward timed text;
- speaker-level mixing/control;
- editing one line does not invalidate the whole episode.

### Cache key

At minimum:

```text
provider/model version
+ voice identity
+ text
+ language
+ synthesis settings
```

A different voice or changed text must invalidate the line, while changing visual crop must not.

## Voice cast registry

The voice cast is application/project data above the provider.

Example:

```text
cast.female_energetic -> provider voice/config A
cast.female_calm      -> provider voice/config B
cast.male_lead        -> provider voice/config C
cast.narrator         -> provider voice/config D
```

Characters map to cast entries. This keeps narrative identity stable even if the underlying TTS provider is upgraded or replaced.

## `SourceProvider` (later)

Content Forge v0.1 should accept uploads and URL records without becoming a scraping framework.

A future source provider may support a specific service/API where technically and contractually appropriate.

Provider responsibilities can include:

- resolve URL metadata;
- obtain source title/creator;
- download/export media where allowed and supported;
- retain canonical source URL.

The project must never assume that a URL is fetchable merely because it was captured in the Inbox.

## `PublishingProvider` (later)

Publishing is deliberately downstream of a completed export.

Potential operations:

```text
upload media
create/update metadata
schedule
read upload status
```

The renderer must not publish as a side effect.

This boundary permits manual publishing, YouTube integration, or future platform integrations without changing project/render semantics.

## `AnalyticsProvider` (later)

After enough real content exists, analytics can import platform performance data and attach it to published variants.

Potential normalized metrics:

```text
views
viewed_vs_swiped
average_view_duration
retention curve
likes/comments/shares
subscriber conversion
restrictions/monetization state where available
revenue/RPM where available
```

Analytics should inform experimentation but not be invented before the production workflow is proven.

## Provider configuration

Local configuration may eventually support:

```yaml
providers:
  llm:
    kind: chatgpt_web_adapter
    enabled: true

  tts:
    kind: qwen_local
    enabled: false

  ocr:
    kind: ...
    enabled: false
```

Secrets/session details must live outside committed config files, ideally through OS/user-local configuration.

## Testing

Core and workflow tests should use fake providers implementing the same protocols.

This allows CI to test:

- suggestion acceptance/rejection;
- provider failure fallback;
- cache behavior;
- deterministic project state;
- dialogue timing with synthetic audio

without needing a live ChatGPT session, GPU TTS model, or external service.
