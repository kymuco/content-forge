# Core domain contracts — PR2

PR2 freezes the first executable data contract for Content Forge. It intentionally
contains no renderer, database, web server, LLM, OCR or TTS implementation.

## Boundary: extensible IDs vs closed states

Plugin-like concepts are registry keys, not closed enums:

- `content_kind`
- `workflow_id`
- `template_id`
- overlay/component types
- audio track types
- motion and transition types
- output profile IDs

This means a future format can register `some_new_format` without adding a new
`ProjectState` or changing the core manifest schema solely to name it.

Closed lifecycle/policy concepts use enums because their semantics are owned by core,
for example `ProjectState`, `ReviewStatus`, `FitMode` and `PermissionStatus`.

## Stable entity IDs

Persisted entities use opaque IDs:

```text
cf_project_<uuid4 hex>
cf_asset_<uuid4 hex>
cf_scene_<uuid4 hex>
...
```

IDs are identity only. File paths, titles, artist names and template labels must never
be used as primary identity.

## Schema version

Every persisted canonical model carries:

```text
schema_version: "1.0"
```

Unknown versions fail validation. A migration layer can be introduced before a future
schema version is accepted.

## Canonical update semantics

Canonical models reject direct field assignment and unknown fields. Dynamic updates
must use `validated_copy(update=...)`, which rebuilds the model through Pydantic
validation after merging the requested changes.

Raw `model_copy(update=...)` is not a canonical update API because Pydantic deliberately
trusts its `update` mapping and does not re-run validation. It may still be useful for
trusted internal copying without updates, but user/provider/runtime changes must not use
it to bypass invariants.

All canonical numeric values must be finite. NaN and positive/negative infinity are
rejected by the model configuration, and JSON serialization additionally uses strict
`allow_nan=False` behavior.

## Profile-independent geometry

All canonical scene/component geometry uses normalized coordinates in `[0, 1]`.

```text
NormalizedRect(x, y, width, height)
NormalizedPoint(x, y)
```

The rectangle is validated not to leave the normalized canvas.

Pixel dimensions belong only to `OutputProfile`:

```text
preview_vertical = 540 x 960
short_vertical   = 1080 x 1920
```

A later render compiler resolves the same semantic timeline separately against each
output profile. Therefore approval of a preview is not approval of a different edit,
and final-canvas pixel coordinates never leak into canonical scene geometry.

This explicitly supersedes the early PR1 wording that described normalized render-plan
geometry in final-canvas pixels.

## Project manifest

`Project` is the canonical aggregate used by the PR2 file serialization tests. It may
contain:

- source asset references and provenance records;
- language/presentation variants;
- workflow/template references;
- scenes;
- project-global overlays and audio tracks;
- output profiles;
- review tasks;
- extension metadata.

PR3 may normalize these objects into SQLite tables without changing their semantic
contract.

## Provenance boundary

`Asset` identifies immutable bytes/metadata. `SourceRecord` describes where those bytes
came from. The same asset can later have multiple source records.

Credit and permission remain separate fields. A creator credit never implies that
permission was granted.

Whenever an `AssetRef` names a `source_id`, the containing project validates that the
source record exists and that it belongs to the same `asset_id`. Dangling or cross-asset
provenance references are invalid because they could attach the wrong credit/permission
metadata to media.

## Human attention boundary

`ReviewTask` is a bounded question attached to a project. It is independent of coarse
project state, so future OCR/speaker/TTS reviews do not require new global states.

Provider suggestions are represented separately from `accepted_value`.

## Serialization

`dump_json` / `load_json` and `dump_yaml` / `load_yaml` round-trip canonical models.
`save_model` performs an atomic same-directory replace for JSON/YAML manifests.

JSON output is standards-compliant and rejects NaN/infinity instead of emitting the
non-standard `NaN` / `Infinity` tokens accepted by Python's permissive default.

Runtime databases and large media remain out of Git.

## Security note for the later API milestone

The phone workflow contains private source media, previews, project metadata and export
artifacts. When the FastAPI service lands, pairing/session authentication must protect
both sensitive reads and writes. Only narrowly scoped health information and the
pairing bootstrap may be public on the LAN.

This closes the ambiguity in the initial PR1 phone-workflow wording; authentication is
not write-only.
