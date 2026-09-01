# PR18 — OCR provider and panel text extraction workflow

PR18 begins Milestone 5 without making OCR part of the renderer or changing the core `Project` schema.

The boundary is deliberately narrow:

```text
verified image asset
        ↓
    OCRProvider
        ↓
raw regions + geometry + confidence + provider evidence
        ↓
retained panel OCR snapshot
        ↓
only uncertain text -> existing ReviewTask
        ↓
raw text + separately accepted correction
```

Speaker attribution, dialogue ordering, TTS, and camera choreography remain later responsibilities.

## Provider contract

`OCRProvider` exposes only:

```python
health() -> OCRProviderHealth
extract(OCRRequest) -> OCRResult
```

An OCR request contains:

- local runtime path used for the actual inference call;
- immutable source SHA-256;
- authoritative source width/height;
- optional language hints.

The semantic request digest intentionally excludes the local path. Moving the same content-addressed asset to another runtime root therefore does not change OCR request identity.

Each result contains zero or more regions with:

- stable extraction-local `region_id`;
- original provider index;
- raw recognized text;
- confidence in `[0, 1]`;
- source-pixel polygon;
- source-pixel axis-aligned bounding box;
- optional language tag;
- provider/model/config evidence.

Region geometry must remain inside the authoritative source dimensions. Non-finite scores or coordinates, mismatched arrays, malformed boxes, and geometry outside the source fail closed.

Provider order is preserved only as evidence. PR18 does **not** claim that provider order is the semantic dialogue/reading order.

## First local provider: PaddleOCR 3.x

The first adapter is `PaddleOCRProvider`.

PR18 targets the PaddleOCR 3.x pipeline surface and is currently developed against the 3.7 generation. The adapter consumes the structured recognition fields exposed by that pipeline:

```text
rec_texts
rec_scores
rec_polys
rec_boxes
```

PaddleOCR commonly exposes geometry/score arrays through NumPy-backed containers, so the adapter normalizes array-like values through their ordinary sequence/`tolist()` representation before validating them into Content Forge models.

Document orientation classification and document unwarping are disabled by the Content Forge adapter. PR18 needs OCR coordinates in the original source-image coordinate space; silently replacing the source geometry with a transformed document coordinate system would break panel-region provenance. Text-line orientation is disabled by default for the same conservative first contract.

The adapter supports the PaddleOCR unified inference-engine option. The default Content Forge intent is the Paddle engine, while the installed local environment may later choose another supported engine deliberately.

### Optional dependency boundary

PaddleOCR and its inference engine are **not** base Content Forge dependencies.

This is intentional:

- renderer/storage/batch operation must remain usable without OCR;
- Paddle/PaddleOCR GPU installation is platform/CUDA-specific;
- alternative OCR providers must remain possible;
- CI can validate the Content Forge adapter against an injected fake runtime without downloading model weights.

A production machine may install a compatible PaddleOCR 3.x environment separately. Provider health reports an unavailable local OCR environment without making unrelated Content Forge operations fail.

## Source integrity

`PanelOCRWorkflow.extract_scene(...)` does not trust a path alone.

Before inference it:

1. resolves the scene's library asset;
2. requires an image asset with authoritative dimensions;
3. verifies the content-addressed source bytes against the stored SHA-256;
4. builds the OCR request from that immutable identity;
5. requires returned source digest/dimensions and request evidence to match the exact request.

The provider therefore cannot silently attach OCR output produced for different bytes to the current panel.

## Durable extraction snapshot

PR18 stores its typed `PanelTextExtraction` under the versioned project metadata namespace:

```text
Project.metadata["pr18_panel_ocr"][scene_id]
```

This avoids changing the persisted core `Project` schema while still making the extraction part of the normal durable project manifest.

A snapshot records:

- project/scene/asset identity;
- source SHA-256 and dimensions;
- confidence threshold used to decide whether review is required;
- raw regions;
- provider/model/config/request evidence.

Once a scene has a retained extraction, PR18 v1 does not silently call OCR again. Repeating the exact same semantic request is idempotent and returns the retained snapshot. A changed language-hint request or review-confidence threshold fails closed and requires an explicit future re-OCR/review-policy migration workflow.

This prevents an expensive model upgrade, changed config, or changed review threshold from silently replacing historical evidence.

## Raw text versus correction

Raw OCR is immutable evidence.

A panel region therefore has two distinct values:

```text
raw_text         # what the OCR provider returned
corrected_text   # optional accepted human correction
```

`effective_text` selects the correction when present but never destroys `raw_text`.

This distinction is required for provenance and debugging: future dialogue/TTS work can know both what the model observed and what a human accepted.

## Confidence and human review

The caller supplies an explicit confidence threshold, defaulting to `0.80` in PR18 v1.

A region is uncertain when:

```text
confidence < threshold
and corrected_text is absent
```

If every region is sufficiently confident, OCR extraction creates no human task.

If any region is uncertain, PR18 creates one existing canonical `ReviewTask`:

```text
task_type = "ocr_text_correction"
attention = REVIEW
priority  = HIGH
blocking  = true
```

Its payload contains only the bounded information needed for correction:

- scene and asset identity;
- digest of the retained extraction;
- exact uncertain region IDs;
- each uncertain region's raw text, confidence, and bounding box.

The workflow does not create a parallel review database or a second authority model.

## Applying corrections

Correction resolution is fail closed:

- task must still be open;
- task must still identify the retained extraction digest;
- caller must provide exactly the current uncertain region IDs;
- corrected strings must be non-empty and bounded;
- closed review cannot be rewritten.

After acceptance, corrected text is written beside raw text and the existing `ReviewTask` is resolved with an accepted-value receipt containing the correction mapping and corrected extraction digest.

## Deliberate boundaries

PR18 does not:

- guess speakers;
- claim OCR provider order is reading order;
- mutate panel crops based on OCR;
- synthesize speech;
- call an LLM automatically;
- erase raw OCR after cleanup;
- make PaddleOCR a renderer dependency;
- silently re-run OCR when provider/model/config/policy changes;
- introduce a second timeline or renderer.

The PR15 `TextCleanupRequest` remains available as a later optional proposal layer. If used, its output must still pass through explicit correction authority rather than overwriting raw OCR.

## Exit condition

PR18 is complete when a verified panel image can be processed through a replaceable local OCR provider, persist raw text/boxes/confidence/evidence, surface only uncertain text as bounded review work, and retain accepted corrections separately from the original OCR result without any speaker-guessing requirement.
