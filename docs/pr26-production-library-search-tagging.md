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

Values are normalized with Unicode NFKC, surrounding/internal whitespace collapse, and Unicode case-folding for lookup identity. Human-facing normalized casing is retained separately. Empty values and Unicode control, format, and surrogate code points are rejected before SQLite encoding.

Tag assignment is asset-level mutable library metadata. Replacing tags does not mutate the Asset manifest or any SourceRecord.

## Search contract

`LibrarySearchQuery` supports:

- exact tags with AND semantics;
- normalized tag-prefix lookup;
- previously-used / not-yet-used filtering;
- bounded `limit` and `offset`.

The hardened index accepts at most 128 exact tags in one query or saved virtual collection. This prevents a bounded HTTP body from expanding into an unbounded SQLite expression tree.

Prefix lookup remains index-friendly. The half-open SQLite range uses the lexicographic successor of the normalized Unicode prefix rather than treating U+FFFF as a maximum suffix, so non-BMP characters such as emoji and the full valid scalar range remain searchable.

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

## Persistence and compatibility

PR26 uses an additive `application_schema` component named `production_library`, version 1. Initialization is atomic and idempotent and fails closed if a newer feature schema is encountered. The base `LibraryDatabase` schema version is unchanged.

The extension is lazy: constructing `LocalLibrary` or installing the API routes does not create PR26 tables. The feature schema is initialized only on first `library.index` use, preserving pre-PR26 application-schema expectations for unrelated workflows.

Tables:

- `library_asset_tags`
- `library_virtual_collections`

## Authenticated API

The local API exposes production-library operations under `/api/v1/production-library`:

- search;
- read/replace asset tags;
- asset reuse history;
- SHA-256 duplicate lookup;
- list/create/read/open/delete virtual collections.

The boundary follows the existing local security model:

- loopback plaintext or HTTPS transport only;
- bearer authentication before JSON parsing;
- exact `application/json` media type for parsed writes;
- required `Content-Length`;
- 64 KiB request-body cap;
- malformed asset/collection identities return 422;
- syntactically valid missing resources return 404;
- excessive exact-tag query complexity returns controlled 422;
- future PR26 schema versions return a controlled schema-unavailable 500 instead of leaking initialization exceptions.

Duplicate lookup deliberately returns `match: null` for a valid SHA-256 that is not present, because “not a duplicate” is a normal lookup result.

## PWA surface

The phone/desktop shell includes a Production Library panel backed only by the authenticated API. The browser does not become a second library authority.

The panel supports:

- exact `kind:value` tags and prefix search;
- used / unused filtering;
- exact tag replacement;
- Project reuse-history inspection;
- virtual collection save/open/delete;
- SHA-256 duplicate checks with source/use warnings.

`production-library.js` is no-cache at the live route and is precached by service-worker shell version v14. The v13 namespace is retained as an upgrade predecessor so installed shells migrate without stale UI authority.

## Validation

PR26 regressions cover:

- normalization and bounded tag kinds;
- indexed exact/prefix tag retrieval;
- AND tag semantics;
- non-BMP and maximum-Unicode-scalar prefix lookup;
- lone-surrogate rejection before SQLite;
- 128-exact-tag query complexity boundary;
- existing SHA dedup integration;
- live previously-used filtering;
- source reuse history from `project_assets`;
- dynamic virtual collections;
- additive lazy schema initialization over an existing library;
- future feature-schema fail-closed behavior;
- authenticated API transport/body/error boundaries;
- PWA panel/script/service-worker v14 regression coverage.
