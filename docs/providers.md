# Providers

## Purpose

Providers isolate capabilities that are useful but should remain replaceable, optional, or environment-specific.

Core project state, timelines, rendering, and storage must not depend directly on a particular LLM, OCR package, TTS model, source website, or publishing API.

The provider families are:

```text
LLMProvider       # implemented in PR15
OCRProvider       # implemented in PR18
TTSProvider       # later
SourceProvider    # later
PublishingProvider # later
AnalyticsProvider # later
```

## General provider rules

Every provider integration should follow these rules where applicable:

1. **Optionality.** Disabling the provider should degrade a feature, not make unrelated rendering/storage fail.
2. **Attribution.** Generated suggestions/artifacts retain provider/model/config metadata when useful for reproduction.
3. **Semantic identity.** Repeatable expensive calls are identified from semantic input rather than machine-local paths/session details.
4. **No silent canonical overwrite.** Suggested or extracted values never replace accepted human values without an explicit workflow rule.
5. **Structured failures.** Provider errors become bounded workflow failures, not corrupted project files.
6. **Secrets stay local.** Cookies, tokens, credentials, model weights, and provider session data are runtime configuration and never repository content.
7. **Narrow interfaces.** Providers expose capabilities the application needs, not their entire underlying SDK/API.
8. **Evidence before mutation.** Provider output is validated before durable project state is updated.

## `LLMProvider`

PR15 implements the task-oriented LLM boundary and the first adapter through `chatgpt-web-adapter`.

The public capabilities are:

```python
class LLMProvider(Protocol):
    def suggest_hooks(self, request: HookRequest) -> HookSuggestions: ...
    def suggest_metadata(self, request: MetadataRequest) -> MetadataSuggestions: ...
    def clean_text(self, request: TextCleanupRequest) -> TextCleanupResult: ...
    def translate(self, request: TranslationRequest) -> TranslationResult: ...
    def classify(self, request: ClassificationRequest) -> ClassificationResult: ...
```

Generated values remain proposals. Existing `ReviewSuggestion` / `ReviewTask` authority decides when a proposal becomes accepted canonical state.

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

PR18 retains raw OCR separately. An LLM may later propose normalized punctuation/spelling through the PR15 text-cleanup contract, but that proposal must never erase raw OCR or bypass OCR correction authority.

#### Translation/localization

Translate accepted hooks/subtitles/metadata into project variants. Translation is stored as variant data with provenance rather than regenerated during every render.

#### Classification/tag suggestions

The LLM can suggest:

- content kind;
- template candidates;
- game/character/topic tags;
- moment type.

These remain convenience features. Asset identity and project validity do not depend on them.

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

Content Forge depends on its own stable request/result models and wraps the external adapter:

```text
Content Forge task request
-> LLMProvider adapter
-> chatgpt-web-adapter
-> model response
-> strict parser/schema/allowlist validation
-> Content Forge proposal result
```

The adapter is optional. Manual fields and deterministic workflows remain usable without a ChatGPT session.

## `OCRProvider`

PR18 freezes the first OCR provider contract:

```python
class OCRProvider(Protocol):
    def health(self) -> OCRProviderHealth: ...
    def extract(self, request: OCRRequest) -> OCRResult: ...
```

The normalized result contains:

```text
regions[]:
  region_id
  provider_index
  bbox
  polygon
  raw_text
  confidence
  language (optional)

evidence:
  provider/version
  model
  engine
  semantic request digest
  provider config digest
```

OCR does **not** solve speaker attribution or dialogue order. Provider ordering is retained as evidence only; PR19 owns reading flow/speaker assignment with explicit human authority where needed.

### Source/integrity boundary

The panel workflow verifies content-addressed source bytes before OCR execution, passes source SHA-256 and authoritative dimensions into the request, and requires returned source identity plus request evidence to match before persisting anything.

Source geometry is pixel-based and fail closed: malformed/non-finite geometry, boxes outside source dimensions, and inconsistent recognition arrays are rejected.

### First local adapter: PaddleOCR

The first implementation is a lazy `PaddleOCRProvider` targeting PaddleOCR 3.x (currently the 3.7 generation).

The adapter consumes structured pipeline fields:

```text
rec_texts
rec_scores
rec_polys
rec_boxes
```

NumPy-backed values are normalized to ordinary sequences and then validated into Content Forge models.

Document orientation classification/unwarping are disabled for the first contract so retained coordinates stay in the original source-image coordinate system. Text-line orientation is also disabled by default. The provider records the selected PaddleOCR model/version/engine/config as evidence.

PaddleOCR and its inference engine are **not** base Content Forge dependencies. A production machine installs its chosen compatible local OCR runtime separately; base rendering/storage/batch behavior remains identical without it.

### Durable OCR result and correction

PR18 stores a versioned per-scene extraction snapshot under project metadata. Raw text remains immutable evidence and human correction is stored separately:

```text
raw_text
corrected_text | None
```

Only regions below the explicit confidence threshold are automatically surfaced as `ocr_text_correction` review work. The retained extraction is idempotent for the same semantic OCR request; different language hints or review policy fail closed instead of silently rerunning/replacing OCR.

Panel-level and review-task budgets bound the amount of provider text/geometry that may enter a project manifest.

PR18's retained project snapshot is the first cache/reuse boundary: reopening or retrying the same scene does not call OCR again. A future cross-project OCR cache can reuse the same semantic request/config evidence without changing this authority model.

See [`pr18-ocr-panel-text.md`](pr18-ocr-panel-text.md) for the complete contract.

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

Content Forge v0.1 accepts uploads and URL records without becoming a scraping framework.

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

  ocr:
    kind: paddleocr_local
    enabled: false

  tts:
    kind: qwen_local
    enabled: false
```

Secrets/session details and local model/runtime configuration live outside committed config files.

## Testing

Core and workflow tests use fake/injected providers implementing the same protocols.

This allows CI to test:

- proposal acceptance/rejection;
- strict provider-output parsing;
- source/request evidence validation;
- raw OCR versus corrected-text retention;
- confidence/review boundaries;
- deterministic project state;
- future dialogue timing with synthetic audio

without needing a live ChatGPT session, downloaded OCR model weights, GPU TTS model, or external service.
