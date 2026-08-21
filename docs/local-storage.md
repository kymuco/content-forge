# Local asset store and SQLite metadata — PR3

PR3 introduces the first persistent local library. The source repository remains code,
documentation, schemas, tests and synthetic fixtures only. Production media and runtime
state live under a separate runtime root.

## Runtime root

`LocalLibrary()` chooses a platform-appropriate local data directory by default:

- Windows: `%LOCALAPPDATA%/ContentForge`;
- macOS: `~/Library/Application Support/ContentForge`;
- Linux: `~/.local/share/content-forge`.

Set `CONTENT_FORGE_HOME` to override this location, or pass an explicit path to
`LocalLibrary(root)` for tests/portable installations.

The runtime layout is:

```text
<root>/
  content-forge.sqlite3
  assets/
    .incoming/
    sha256/
      ab/
        cd/
          <full-sha256>
```

The blob filename is the full SHA-256 digest and has no semantic filename dependency.
Original names, URLs, creators and titles belong to metadata/provenance rather than
asset identity.

## Ingest contract

`AssetStore.ingest_file()` streams the input once into a unique staging file while
computing SHA-256. The staging file is fsynced before it is atomically moved into its
content-addressed location.

If the same bytes are ingested again:

- the existing `Asset` identity is reused;
- the blob is not duplicated;
- a new `SourceRecord` may still be retained, because identical bytes can have multiple
  acquisition locations or attribution contexts.

The storage layer verifies an already-present blob before trusting it. A size/digest
mismatch raises `AssetIntegrityError` rather than silently replacing corrupted state.

PR3 deliberately does not inspect codecs, dimensions, duration or FPS. Those slots
already exist on `Asset`, but authoritative media probing belongs to the ffprobe backend
in PR5.

## Provenance

`SourceInput` is the ingest-time record used before the immutable asset ID is known. It
supports:

- source URL;
- platform;
- creator/artist name and handle;
- original source title;
- acquisition timestamp;
- visible credit text / credit requirement;
- permission status and notes;
- free-form notes.

After hashing/deduplication, Content Forge creates a canonical `SourceRecord` tied to the
stored `asset_id`.

Credit and permission remain separate. A credit string is not evidence of permission.

## SQLite catalog

Schema version `1` stores:

- canonical assets;
- provenance/source records;
- project manifests;
- normalized project-to-asset references;
- lightweight persistent job metadata;
- thumbnail/proxy derivative metadata slots.

SQLite foreign keys are enabled. Project persistence refuses unknown asset references
and validates stored `source_id -> asset_id` provenance links before committing the
manifest.

Project manifests remain canonical PR2 JSON. SQLite is an index/persistence layer, not a
second competing project schema.

## Atomicity and concurrency

Blob staging uses a unique temporary file per ingest. SQLite writes use short
`BEGIN IMMEDIATE` transactions, WAL mode and a busy timeout. Concurrent duplicate
imports converge on the SHA-256 uniqueness constraint and return the already-persisted
asset identity.

Job execution is not implemented in PR3. The `jobs` table only freezes the metadata
boundary needed by later workers.

## Repository hygiene

`.gitignore` explicitly excludes common local runtime paths, but the primary safety
boundary is architectural: the default runtime root is outside the checkout.

Never add production videos, manga/manhwa pages, fan art, game footage, downloaded
music, source cookies, credentials, previews, exports or the runtime SQLite database to
this public repository.
