# PR20 — TTS provider and Qwen3 per-line synthesis

## Purpose

PR20 adds reusable speech generation to the accepted PR19 dialogue layer without making one TTS model, one GPU runtime, or one voice-cast scheme part of Content Forge core.

The authority chain is:

```text
verified PR18 OCR
-> accepted PR19 dialogue line
-> explicit PR20 line synthesis settings
-> TTSProvider
-> independently verified PCM16 WAV
-> content-addressed Asset
-> durable PR20 line synthesis receipt/cache
```

PR20 does **not** yet attach generated speech to canonical scene `audio_tracks`. Dialogue sequencing, pauses, music/ambience ducking, and final voiced-scene composition remain PR23 responsibilities.

Persistent character-to-voice casting remains PR21. PR20 deliberately accepts an explicit `voice_id` per synthesis request so the provider/cache boundary exists before a global/project cast registry is introduced.

## Provider contract

The model-agnostic public surface is:

```python
class TTSProvider(Protocol):
    def health(self) -> TTSProviderHealth: ...
    def synthesize(self, request: TTSRequest) -> TTSResult: ...
```

`TTSRequest` contains:

- exact accepted line text;
- optional BCP-47 language tag;
- explicit voice identifier;
- optional natural-language style/instruction;
- optional verified reference audio plus its exact SHA-256 and transcript;
- bounded generation settings;
- one caller-selected output path.

The output path and reference-audio **local path** are runtime details and are excluded from semantic request identity. Reference-audio SHA-256, transcript, and clone mode are semantic and therefore do participate in identity.

`TTSResult` describes one provider-produced WAV:

- audio SHA-256 and exact byte size;
- sample rate;
- channel count;
- sample/frame count;
- duration;
- provider/model/revision/config/request evidence;
- resolved voice/language evidence.

The application never trusts those claims blindly. The resulting file is opened independently with Python's WAV reader and must be a bounded, uncompressed PCM16 WAV whose bytes, sample geometry, duration, and SHA-256 agree with the provider result before it can be ingested.

## Semantic identity and cache

PR20 separates two identities:

```text
semantic request digest
= text
+ language
+ voice_id
+ instruction
+ reference audio SHA-256/transcript/mode
+ generation settings

line cache key
= semantic request digest
+ provider_id
+ provider_version
+ model_id
+ model_revision
+ provider config SHA-256
```

This means:

- moving runtime files does not invalidate a line;
- changing the accepted text invalidates it;
- changing voice, language, style, clone reference, or generation settings invalidates it;
- changing provider/model/model revision/config invalidates it;
- visual crop/template changes do not invalidate line audio by themselves.

This is **deterministic cache invalidation**, not a claim that a stochastic GPU synthesis rerun is bit-identical. Once accepted into the cache, the exact generated WAV bytes are content-addressed and reused by SHA-256. Regeneration after deliberate invalidation may produce different bytes even for semantically similar settings unless the chosen provider/runtime itself guarantees stronger determinism.

## Accepted PR19 authority

PR20 may synthesize only a dialogue line that survives the complete PR19 integrity check against:

- current scene media;
- retained PR18 OCR extraction;
- accepted reading order and speaker assignment;
- unique resolved PR19 review evidence;
- accepted `scene_dialogue_digest`.

PR20 exposes the existing PR19 validation as an exact-snapshot helper so line generation is based on the same immutable `Project` snapshot that later participates in the PR20 compare-and-swap write.

Each durable `SynthesizedDialogueLine` stores:

- `scene_id` / `line_id`;
- exact accepted `scene_dialogue_digest`;
- exact accepted source text and narrative `speaker_id`;
- explicit line synthesis settings;
- semantic cache key;
- complete TTS invocation evidence including model revision;
- generated `asset_id`, SHA-256, size, duration, sample rate, channels, and sample count.

A persisted PR20 record is revalidated against current accepted dialogue and its request/cache identity before it can be reused. Public manifest reads additionally re-hash and reopen the generated WAV.

## Concurrency and failure boundary

Synthesis can be expensive, so PR20 snapshots the exact serialized Project before the provider call and publishes metadata using a persisted-JSON compare-and-swap afterward.

If another request changes the Project while synthesis is running:

- the generated audio may already have entered the immutable content-addressed asset store;
- the stale PR20 Project update is rejected;
- no newer Project state is overwritten;
- an unreferenced content-addressed blob is harmless and can be reused or garbage-collected by a future maintenance facility.

Provider failures, malformed output, hash mismatches, invalid WAV geometry, missing/corrupt reference assets, and stale dialogue all fail before PR20 state is authorized.

## Qwen3-TTS adapter

The first local implementation targets the official Qwen3-TTS family through the optional `qwen-tts` Python package. PR20 currently supports the package's 0.1.x API family and keeps it outside base dependencies:

```text
pip install 'content-forge[tts]'
```

The adapter uses the official `Qwen3TTSModel` surface and has three explicit modes:

```text
custom_voice -> Qwen3-TTS-...-CustomVoice -> generate_custom_voice(...)
voice_clone  -> Qwen3-TTS-...-Base        -> generate_voice_clone(...)
voice_design -> Qwen3-TTS-...-VoiceDesign -> generate_voice_design(...)
```

A config/model name that does not match the selected mode is rejected before inference. This avoids accidentally treating a Base checkpoint as a built-in voice model or vice versa.

The released Qwen3-TTS 12Hz family supports Chinese, English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, and Italian. Content Forge maps the base subtag of the supplied BCP-47 language (for example `en-US -> English`) and uses Qwen's `Auto` mode when no language or `und` is supplied.

### Immutable checkpoint resolution

A Qwen model name alone is not sufficient reproducibility evidence because a Hub repository's default branch can move. PR20 therefore requires an immutable 40-hex Hugging Face commit SHA as `revision`, retains it separately in provider evidence, and includes it in cache identity.

Content Forge resolves the **entire model repository snapshot** through `huggingface_hub.snapshot_download(repo_id=..., revision=...)` before constructing Qwen. Qwen then receives the resolved local snapshot directory instead of the floating repository ID.

This is intentionally stronger than passing `revision=` directly to the current upstream `Qwen3TTSModel.from_pretrained(...)`: the upstream wrapper forwards model kwargs to `AutoModel`, while its processor load is performed separately. Resolving the whole snapshot first guarantees that model weights, processor, tokenizer/config, and generation files all originate from the same immutable repository commit.

The default lightweight adapter configuration is:

```text
Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
revision=85e237c12c027371202489a0ec509ded67b5e4b5
mode=custom_voice
device_map=auto
dtype=bfloat16
```

Selecting a different Qwen repository requires supplying its own explicit commit SHA; PR20 does not silently reuse the default model's revision for another repository.

FlashAttention is not a Content Forge requirement. A local installation may opt into an `attn_implementation` supported by its hardware/runtime; that choice is retained in the provider config digest.

### 0.6B CustomVoice instruction boundary

The upstream Qwen3-TTS 0.6B CustomVoice implementation does not apply `instruct` control; it explicitly discards the instruction. PR20 therefore rejects a non-empty instruction for a 0.6B CustomVoice model instead of pretending the requested style was honored.

Instruction-controlled `custom_voice` synthesis requires a compatible 1.7B CustomVoice checkpoint with its own explicit pinned revision. `voice_design` remains the separate free-form voice-description capability.

### Lazy loading

`QwenTTSProvider.health()` resolves package/config/cache identity without loading multi-gigabyte model weights. This is important because an exact PR20 cache hit should not initialize CUDA merely to discover that synthesis is unnecessary.

The pinned repository snapshot and actual `Qwen3TTSModel.from_pretrained(...)` model load happen only on the first cache miss that requires synthesis.

### Output normalization

Qwen returns waveform samples plus a sample rate. Content Forge converts one returned line waveform to a standard mono PCM16 WAV itself using the Python standard library. This intentionally keeps the durable artifact format and independent verification boundary outside provider-specific convenience writers.

## Voice cloning

For `voice_clone`, `LineTTSSettings.reference_asset_id` must resolve to an existing Content Forge audio asset. Its canonical bytes are verified before the provider sees them.

The semantic request includes:

- reference asset SHA-256;
- optional reference transcript;
- `x_vector_only_mode`.

If `x_vector_only_mode` is false, the Qwen adapter requires reference text. When it is true, transcript-free embedding-only cloning is allowed, matching the Qwen3-TTS API boundary while explicitly retaining that weaker clone mode in cache identity.

## Voice design and PR21

`voice_design` is exposed as a provider capability, but PR20 does not pretend that a free-form design prompt is already a persistent cast identity. The request still carries an explicit application-local `voice_id` label and the design instruction is part of the semantic cache key.

PR21 can later turn an approved voice configuration/reference/design into a persistent Voice Cast entry without changing the PR20 synthesis/cache contract.

## Storage boundary

Generated speech is stored as a normal immutable Content Forge `Asset` with `MediaType.AUDIO` and `audio/wav` MIME intent. The PR20 Project metadata retains the asset ID and exact digest/evidence.

PR20 intentionally does not add those line assets to canonical Scene audio tracks yet. That would prematurely define ordering, pauses, overlap, and mix semantics that belong to PR23.

## Testing

Normal CI does not install `qwen-tts`, Torch, model weights, or require a GPU.

Provider tests inject fake Qwen runtimes and verify:

- lazy model construction;
- mode/model validation;
- immutable revision validation and revision-sensitive cache identity;
- complete repository snapshot resolution at the exact commit;
- language mapping;
- custom voice, clone, and design call shapes;
- fail-closed 0.6B CustomVoice instruction handling;
- reference-audio digest verification;
- generation-kwarg forwarding;
- one-waveform/finite-sample boundaries;
- standard PCM16 WAV publication;
- path-independent semantic identity.

Application tests use a fake TTS provider over a real accepted PR18 -> PR19 fixture and verify:

- first synthesis and content-addressed audio ingest;
- exact cache reuse without a second provider call;
- voice/style invalidation;
- reference-asset integrity;
- stored speaker/source tampering rejection;
- generated blob corruption rejection;
- provider evidence mismatch rejection;
- Project CAS failure when state changes during expensive synthesis.

## Deferred scope

PR20 does not include:

- persistent character-to-cast mapping (PR21);
- phone/desktop listen, regenerate, or voice selection UI (PR21/PR22);
- automatic scene duration mutation from synthesized lines (PR22);
- phrase-level timed text/ASS (PR22);
- dialogue ordering/pauses in the audio timeline (PR23);
- ambience/music ducking around speech (PR23);
- camera choreography from PR19 focus hints (PR23);
- live streaming TTS playback/runtime scheduling.

Those layers consume the durable PR20 line artifacts rather than redefining synthesis identity.

## Upstream references

- Qwen3-TTS repository: <https://github.com/QwenLM/Qwen3-TTS>
- Qwen3-TTS 0.6B CustomVoice model: <https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice>
