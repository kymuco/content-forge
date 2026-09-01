# Providers

## Purpose

Providers isolate capabilities that are useful but should remain replaceable, optional, or environment-specific.

Core project state, timelines, rendering, and storage must not depend directly on a particular LLM, OCR package, TTS model, source website, or publishing API.

The provider families are:

```text
LLMProvider        # implemented in PR15
OCRProvider        # implemented in PR18
TTSProvider        # implemented in PR20
SourceProvider     # later
PublishingProvider # later
AnalyticsProvider  # later
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

PR18 retains raw OCR separately. An LLM may propose normalized punctuation/spelling through the PR15 text-cleanup contract, but that proposal must never erase raw OCR or bypass OCR correction authority.

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

The first implementation is a lazy `PaddleOCRProvider` targeting PaddleOCR 3.x.

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

## `TTSProvider`

PR20 freezes a model-agnostic line-synthesis boundary:

```python
class TTSProvider(Protocol):
    def health(self) -> TTSProviderHealth: ...
    def synthesize(self, request: TTSRequest) -> TTSResult: ...
```

A request contains exact accepted dialogue text plus explicit language/voice/style intent, optional verified voice-reference audio, and bounded generation settings. The provider writes one line artifact to a caller-owned runtime path and returns bounded evidence describing those bytes.

The semantic request digest deliberately excludes local output/reference paths. It includes:

```text
accepted text
language
voice_id
instruction/style
reference audio SHA-256 + transcript + clone mode
generation settings
```

The reusable line cache key then binds that semantic digest to:

```text
provider_id
provider_version
model_id
provider config SHA-256
```

This gives deterministic cache invalidation. It does **not** claim that a stochastic TTS model will reproduce bit-identical bytes after the cached artifact is deliberately discarded and regenerated.

### Per-line synthesis

Dialogue is synthesized one accepted PR19 line at a time rather than as one scene waveform.

Benefits:

- exact cache reuse;
- isolated regeneration;
- known line durations;
- straightforward later timed text;
- speaker-level control;
- editing or changing the voice of one line does not force regeneration of an entire episode.

Every durable PR20 line receipt retains the exact PR19 scene-dialogue digest, accepted source text, narrative speaker ID, synthesis settings, provider/model/config/request evidence, generated audio asset identity, sample geometry, and duration.

### Independent artifact verification

A provider result does not authorize project state merely because its schema validates.

Before publication, Content Forge independently reopens the generated file and requires a bounded uncompressed PCM16 WAV whose:

- SHA-256;
- byte size;
- sample rate;
- channel count;
- sample/frame count;
- duration

match the provider result. Only then are the bytes ingested into the normal content-addressed `AssetStore`.

Cache reads revalidate PR19 source authority and semantic request/cache identity. Public PR20 manifest reads also re-hash/reopen retained generated audio, so post-publication byte or metadata tampering fails closed.

### PR19 authority and concurrency

PR20 synthesizes only dialogue that still passes the complete accepted PR19 provenance/review check. PR19 exposes that validation over one exact `Project` snapshot so PR20 does not introduce a second-snapshot TOCTOU between source validation and synthesis planning.

The Project snapshot used to plan the provider request is retained as exact serialized JSON. After expensive generation and artifact verification, PR20 updates Project metadata with compare-and-swap. If the Project changed concurrently, the stale write is rejected rather than overwriting newer state.

### First local adapter: Qwen3-TTS

The first implementation is `QwenTTSProvider`, a lazy adapter for the official Qwen3-TTS `qwen-tts` 0.1.x package/API.

It supports three explicit model modes:

```text
custom_voice -> ...-CustomVoice -> generate_custom_voice
voice_clone  -> ...-Base        -> generate_voice_clone
voice_design -> ...-VoiceDesign -> generate_voice_design
```

The configured checkpoint name must match the selected mode. This avoids silently invoking the wrong Qwen capability family.

The released 12Hz Qwen3-TTS family supports Chinese, English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, and Italian. PR20 maps BCP-47 base subtags to the Qwen language names and uses Qwen `Auto` when language is omitted or `und`.

The default adapter configuration uses the lighter `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` checkpoint. Qwen model weights, Torch/CUDA, and the `qwen-tts` package remain optional local runtime data/dependencies and are not installed by base Content Forge.

Install the optional adapter environment with:

```text
pip install 'content-forge[tts]'
```

`health()` resolves package/config/cache identity without loading model weights; actual `Qwen3TTSModel.from_pretrained(...)` construction is lazy on the first cache miss. This allows cached projects to remain cheap to inspect/use.

Qwen waveform samples are normalized by Content Forge into a standard mono PCM16 WAV using the Python standard library, keeping durable artifact encoding outside provider convenience writers.

### Voice cloning

`voice_clone` requires a Content Forge audio asset as the reference. The asset's content-addressed bytes are verified before provider execution. Reference SHA-256, transcript, and `x_vector_only_mode` all participate in request identity.

When `x_vector_only_mode=False`, the Qwen adapter requires reference transcript text. Embedding-only cloning can omit the transcript only when the weaker `x_vector_only_mode=True` is explicit.

### Voice cast registry

Persistent voice casting remains the application layer above the provider and is PR21 scope.

PR20 accepts an explicit line `voice_id`; PR21 can later define stable entries such as:

```text
cast.female_energetic -> provider voice/config A
cast.female_calm      -> provider voice/config B
cast.male_lead        -> provider voice/config C
cast.narrator         -> provider voice/config D
```

Characters can then map to cast entries without changing the PR20 request/cache/artifact contract or tying narrative identity to one TTS engine.

See [`pr20-tts-qwen.md`](pr20-tts-qwen.md) for the complete PR20 contract.

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
    kind: qwen3_tts_local
    enabled: false
```

Secrets/session details, model weights, and machine-local runtime configuration live outside committed config files.

## Testing

Core and workflow tests use fake/injected providers implementing the same protocols.

This allows normal CI to test:

- proposal acceptance/rejection;
- strict provider-output parsing;
- source/request evidence validation;
- raw OCR versus corrected-text retention;
- confidence/review boundaries;
- deterministic project state;
- PR20 per-line synthesis identity/cache invalidation;
- Qwen call-shape/language/mode boundaries;
- PCM16 WAV verification and generated-asset integrity;
- future dialogue timing with synthetic audio

without needing a live ChatGPT session, downloaded OCR model weights, Qwen/Torch model weights, a GPU, or external service.
