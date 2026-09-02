# PR32 — Phone create-video wizard and human-facing presets

PR32 turns the PR31 Production Home into a real phone-first video creation entry point without adding a second project, renderer, or review state machine.

## Product flow

```text
prepared Inbox media
-> Create video
-> choose a human-facing preset
-> choose compatible sources
-> choose their order
-> create one production Project
-> existing Review / Preview / Approve / Final authority
```

The desktop remains the local source of truth and compute worker. The phone selects production intent and supplies bounded human decisions.

## Built-in phone presets

PR32 exposes five human-facing presets backed by existing registered templates:

| Preset | Existing template | Source contract |
| --- | --- | --- |
| Hook Short | `hook_overlay@1.0` | 1–16 images/videos; reviewed hook and crop |
| Top Bar Short | `hook_topbar@1.0` | 1–16 images/videos; reviewed hook and crop |
| Framed Clip | `content_frame@1.0` | 1–16 images/videos |
| Art Story | `art_story@1.0` | 1–32 images; required credits must already be usable |
| Panel Story | `panel_sequence@1.0` | 1–64 images |

The labels are product vocabulary only. They do not create new renderer semantics. Each preset binds to one exact template/version already owned by the template registry.

## Source authority

The wizard reads `/api/v1/production/sources`, which is derived from canonical Inbox Projects and authoritative stored `Asset` metadata. Client MIME hints are not used to decide whether a source is an image or video.

A selectable source must:

- be an original Inbox Project;
- contain exactly one source asset reference;
- reference an existing image/video Asset;
- for video, have positive duration and complete authoritative `has_audio` probe metadata;
- retain consistent source provenance when a `source_id` is present.

Art Story rejects a source with `requires_credit=true` and missing/blank `credit_text` before project creation rather than deferring that failure to render time.

Catalog limits are applied after eligibility filtering. Recent unrelated or malformed Projects therefore cannot consume the caller's valid-source/result budget.

## Durable create identity

The phone supplies a canonical UUID `request_id`. PR32 derives one deterministic Project ID:

```text
cf_project_<first 32 hex chars of
  SHA256("content-forge-production-project-v1\0" + canonical_request_uuid)>
```

This is retry identity, not content identity. The same media may intentionally be used again by creating a new request UUID.

A replay with the same request UUID and the same preset plus ordered source-project selection returns the existing Project. Reusing the UUID for different input fails closed.

The API validates the UUID at the request boundary, so malformed retry identities fail with request validation rather than becoming internal errors.

## Exact wizard-selection evidence

`Project.metadata["production_preset_v1"]` is a canonical JSON **string**, not a nested metadata mapping. Keeping the evidence scalar preserves compatibility with historical fully validated Project mutation paths that deep-freeze nested JSON values.

The canonical evidence records:

```json
{
  "schema_version": 1,
  "request_id": "<canonical UUID>",
  "preset_id": "framed_clip",
  "template_id": "content_frame",
  "template_version": "1.0",
  "sources": [
    {
      "source_project_id": "cf_project_...",
      "asset_id": "cf_asset_...",
      "source_id": "cf_source_... or null"
    }
  ]
}
```

The `sources` list is ordered. Validation proves that it still matches:

- the deterministic production Project ID;
- exact preset/template identity;
- `Project.source_refs` in the selected order;
- Project scenes in contiguous order;
- each scene media asset/source identity;
- retained provenance records for non-null source IDs.

A Project whose scenes/source refs are later tampered away from the wizard selection is rejected as invalid PR32 authority and is quarantined from the trusted production-project catalog.

## Review and render reuse

PR32-created Projects enter the existing PR10 lifecycle immediately. The wizard does not create an independent "wizard state" after Project creation.

Preset-aware bootstrap creates only the review tasks relevant to the selected format:

- hook task only for presets that require a hook;
- crop confirmation only for presets whose current contract exposes crop review;
- metadata review;
- preview approval.

There is no redundant `source_order` task: source ordering was already an explicit human action in the wizard and is frozen into PR32 evidence.

Non-`hook_overlay` presets compile through the existing generic registered-template path. The existing voiced-story guard remains in the Review service chain, and PR23/PR24 exact-snapshot presentation/shared-scene guards are retained before generic compilation. Preview/final rendering, artifact authentication, approval revision identity, QC, and recovery remain owned by the existing Review/Render runtime.

## Phone/PWA behavior

PR31 Production Home gains a Create video wizard rather than a second application shell. It:

- loads the exact preset catalog from the authenticated API;
- filters sources by preset compatibility;
- lets the user select and explicitly order media;
- creates the production Project;
- refreshes the existing Home/Review surfaces and continues into normal review.

Derived PR32 Projects are available through `/api/v1/production/projects`, so they remain visible on Home after they leave Inbox and after final completion.

The controller continues to avoid `innerHTML`; authenticated thumbnails and final artifacts use existing protected endpoints.

The installed PWA cache advances from v17 to v18. v16 and v17 remain explicit predecessor namespaces so existing installed shells upgrade deterministically.

## Security and failure behavior

Production routes retain the local transport policy and require bearer authentication before JSON parsing. POST bodies require JSON, Content-Length, and a bounded 32 KiB body.

PR32 does not accept credentials, filesystem paths, provider state, or arbitrary template IDs from the phone create request. The client chooses only a published preset ID and existing source Project IDs.

Malformed/tampered preset Projects are not silently repaired or treated as trusted catalog entries. Unusable media is rejected before production Project creation where the required authority can be proven locally.

## Non-goals

PR32 does not add:

- a general-purpose mobile NLE/timeline editor;
- new FFmpeg/render semantics;
- a second project/review state machine;
- automatic publishing;
- analytics or recommendation authority;
- arbitrary user-supplied template/version selection;
- hidden source-order inference.

PR33 owns the tighter project-specific edit/preview/final phone experience over the authority established here.
