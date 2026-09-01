# PR26 — Production library search and tagging

## Goal

Add durable organization and retrieval over the existing local Content Forge library without changing immutable Asset identity, SourceRecord provenance, Project authority, or physical content-addressed storage.

PR26 is an index/query layer, not a second media library.

## Authority boundaries

- `Asset` remains immutable byte/media identity.
- `SourceRecord` remains immutable provenance and permission/credit evidence.
- `Project` plus `project_assets` remains current production-use authority.
- PR26 owns mutable organizational tags and virtual saved queries only.
- PR26 never moves or duplicates asset blobs to represent collections.
- duplicate detection delegates to the existing unique SHA-256 Asset identity.
- previous-use warnings and reuse history are derived from `project_assets`; PR26 does not persist a second usage ledger.

## Tag contract

Supported tag kinds are deliberately bounded:

- `game`
- `anime`
- `artist`
- `character`
- `topic`
- `source`

Values are normalized with Unicode NFKC, surrounding/internal whitespace collapse, and Unicode case-folding for lookup identity. Human-facing normalized casing is retained separately. Empty values and control/format characters are rejected.

Tag assignment is asset-level mutable library metadata. Replacing tags does not mutate the Asset manifest or any SourceRecord.

## Search contract

`LibrarySearchQuery` supports:

- exact tags with AND semantics;
- normalized tag-prefix lookup;
- previously-used / not-yet-used filtering;
- bounded `limit` and `offset`.

Tag lookup uses SQLite indices rather than filesystem traversal. Search hits enrich the immutable Asset with current tags, source count, and Project-use count.

## Duplicate warning

`duplicate_info(sha256)` uses the existing `assets.sha256 UNIQUE` identity. Multiple ingests of identical bytes therefore remain one Asset with potentially multiple SourceRecords. PR26 reports source count and Project-use count; it does not create a second duplicate definition.

## Source reuse history

`reuse_history(asset_id)` joins the existing `project_assets` index to current Project rows and reports Project ID, content kind/state, source ID, role, and Project update time. Because the query is live, saving a Project is already sufficient to update PR26 reuse results.

## Virtual collections

A virtual collection stores only:

- a readable collection ID;
- display name;
- a validated `LibrarySearchQuery`;
- timestamps.

Its members are resolved dynamically from the current tag/use index. No asset copy, folder move, or persisted membership list is created.

## Persistence

PR26 uses an additive `application_schema` component named `production_library`, version 1. Initialization is atomic and idempotent and fails closed if a newer feature schema is encountered. The base `LibraryDatabase` schema version is unchanged.

Tables:

- `library_asset_tags`
- `library_virtual_collections`

## Initial validation

The first vertical slice must prove:

- normalization and bounded tag kinds;
- indexed exact/prefix tag retrieval;
- AND tag semantics;
- existing SHA dedup integration;
- live previously-used filtering;
- source reuse history from `project_assets`;
- dynamic virtual collections;
- additive schema initialization over an existing library;
- future feature-schema fail-closed behavior.

API/PWA browsing and editing are follow-up layers after this storage contract is green.
