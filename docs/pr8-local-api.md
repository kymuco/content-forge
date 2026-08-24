# PR8 local API and Inbox contract

PR8 is the first application boundary above the existing Content Forge core/storage/render stack. FastAPI is a transport adapter; route handlers do not own SQLite, asset, ffprobe, thumbnail, timeline, or render semantics.

```text
HTTP / future PWA / future CLI
            |
         FastAPI
            |
      application services
       /       |       \
    auth     inbox    media prep
       \       |       /
       LocalLibrary / SQLite
            |
 AssetStore + ffprobe + derivatives
            |
       Project(INBOX)
```

## Public bootstrap endpoints

Only narrow bootstrap operations are unauthenticated:

- `GET /health`
- `POST /api/v1/pairing/exchange`

`POST /api/v1/pairing/challenges` is additionally restricted to loopback clients. A challenge contains an opaque challenge ID plus an eight-digit short-lived code. The database stores a salted digest, not the plaintext code. Challenges are one-time and have a bounded failed-attempt budget.

Successful exchange returns a high-entropy bearer token. Only its SHA-256 digest is persisted. Sessions expire and can be revoked with `DELETE /api/v1/sessions/current`.

## Authenticated Inbox endpoints

- `GET /api/v1/inbox`
- `GET /api/v1/inbox/{intake_id}`
- `POST /api/v1/inbox/files`
- `POST /api/v1/inbox/url-note`
- `GET /api/v1/assets/{asset_id}/thumbnail`

The API returns application IDs and safe metadata only. It does not expose absolute runtime paths, SQLite locations, provider credentials, command execution, or raw session digests.

## File ingest semantics

A multipart upload follows this boundary:

```text
create durable intake receipt
-> bounded/fsynced HTTP staging
-> existing AssetStore ingest + SHA-256 deduplication
-> source/provenance record
-> ffprobe
-> persist allowed probe metadata enrichment
-> thumbnail derivative for visual media
-> Project(state=INBOX)
-> terminal intake state
```

A probe or thumbnail failure does not delete an already accepted immutable asset. The intake becomes `partial`, retains the project/asset identity, and records the preparation error. A failure before an asset can be accepted becomes `failed`.

The file-size limit is enforced while copying the upload stream and never requires loading the complete file into memory.

`application/octet-stream` does not override filename-based MIME inference, because mobile clients frequently use the generic content type for otherwise recognizable files.

## URL/note capture

URL/note intake deliberately does not create a fake `Asset` or fake `SourceRecord` when no bytes exist. It creates a durable Inbox intake plus an assetless `Project(INBOX)` whose metadata preserves the captured URL/note/creator hint. A future source provider can resolve bytes later without changing this boundary.

## Thumbnail derivative

PR8 writes a deterministic JPEG derivative under a runtime-relative key derived from source SHA-256 and thumbnail spec, then records it through the existing `DerivativeSlot` mechanism. Publication is atomic: FFmpeg writes a same-directory temporary JPEG and the final path appears only after a non-empty output is produced and fsynced.

## Explicit non-goals

PR8 does not add:

- a PWA or mobile UI;
- a public-internet deployment mode;
- a worker pool or batch scheduler;
- automatic source downloading;
- direct render execution from HTTP handlers;
- review-task UI;
- publishing.

Those remain later roadmap layers over this application boundary.
