# Development

PR2 introduces the first installable Python package and deterministic test baseline.

## Requirements

- Python 3.11+
- no FFmpeg/GPU/LLM/TTS requirement for PR2

## Install

```bash
python -m pip install -e ".[dev]"
```

## Test

```bash
pytest -q
```

The CI matrix runs the same deterministic suite on Python 3.11, 3.12 and 3.13.

## Current boundary

The package currently contains domain contracts and serialization only. It does not
render media, touch SQLite, start a web server, call an LLM, run OCR, or synthesize
speech.

That is intentional. PR3 adds local asset persistence; later PRs add timeline
compilation and rendering.

## Model evolution

Canonical models reject unknown fields and direct assignment. JSON-compatible nested
containers are recursively frozen as well, so ordinary `dict`/`list` mutation cannot
silently bypass validation after construction.

Apply canonical copy-on-write changes with `validated_copy()` so Pydantic re-runs both
field and model validation:

```python
updated = project.validated_copy(update={"state": ProjectState.READY})
```

Do not use raw `model_copy(update=...)` for dynamic or user-supplied canonical changes:
Pydantic intentionally trusts those updates without revalidation.

Persisted models carry `schema_version: "1.0"`. Do not silently accept a future schema
version; add migration support first.

Canonical numeric values must be finite; NaN and infinities are rejected. JSON output
also uses strict standards-compliant serialization as a defensive boundary.

Entity IDs are globally unique within their entity family across one project aggregate:
scene-local overlays/audio and review suggestions cannot reuse IDs found elsewhere in
the same project. This keeps future ID-based updates and SQLite normalization
unambiguous.

Manifest saves use a unique same-directory temporary file per invocation followed by
`os.replace`. Concurrent writers remain last-writer-wins, but one save cannot steal or
move another save's temporary file.

## Fixtures

Tests and examples must remain synthetic/redistributable. Do not add production videos,
manga/manhwa pages, fan art, game footage, provider cookies, credentials, local
databases, generated voice datasets, previews or exports to this public repository.
