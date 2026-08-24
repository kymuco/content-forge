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

## Transport and bootstrap security

Plain HTTP is supported only for loopback development/use. Any non-loopback bind, including `--lan`, requires a TLS certificate and private key; the server refuses to start otherwise. The certificate must be trusted by the phone and valid for the address used by the client.

Only narrow bootstrap operations are unauthenticated:

- `GET /health`
- `POST /api/v1/pairing/exchange`

`POST /api/v1/pairing/challenges` is restricted to a loopback socket peer and also validates loopback `Host` plus browser `Origin` when one is present. This prevents a hostile browser origin or DNS-rebound hostname from using the loopback socket as proof of desktop user presence.

A challenge contains an opaque challenge ID plus an eight-digit short-lived code. The database stores a salted digest, not the plaintext code. Challenges are one-time and have a bounded failed-attempt budget.

Successful exchange returns a high-entropy bearer token. Only its SHA-256 digest is persisted. Sessions expire and can be revoked with `DELETE /api/v1/sessions/current`.

## Authenticated Inbox endpoints

- `GET /api/v1/inbox`
- `GET /api/v1/inbox/{intake_id}`
- `POST /api/v1/inbox/files`
- `POST /api/v1/inbox/url-note`
- `GET /api/v1/assets/{asset_id}/thumbnail`

The API returns application IDs and safe metadata only. It does not expose absolute runtime paths, SQLite locations, provider credentials, command execution, raw subprocess diagnostics, or stored session digests.

## File ingest semantics

A multipart upload follows this boundary:

```text
pre-parse bearer authentication + Content-Length bound
-> create durable intake receipt
-> bounded/fsynced application staging
-> existing AssetStore ingest + SHA-256 deduplication
-> source/provenance record
-> durable asset/project linkage checkpoint
-> ffprobe
-> authoritative media classification + metadata enrichment
-> thumbnail derivative for visual media
-> terminal intake state
```

FastAPI/Starlette would normally parse/spool `UploadFile` before endpoint dependencies. PR8 therefore guards the upload route in middleware before multipart parsing: authentication is checked first, `Content-Length` is mandatory, and the complete multipart body is capped before Starlette consumes it. The application staging copy independently enforces the actual file-byte limit.

Client-provided multipart MIME type and filename remain provenance/UI hints only. A new uploaded Asset begins with neutral `OTHER` / `application/octet-stream` classification; a successful ffprobe is the authority allowed to promote those shared fields. A later intake cannot reclassify an already-classified deduplicated asset.

A probe or thumbnail failure does not delete an already accepted immutable asset. The intake becomes `partial`, retains the project/asset identity, and records a path-safe diagnostic. Unexpected failures preserve durable links and fail closed without publishing raw subprocess/storage exception text.

## Interruption recovery

Application startup reconciles any intake left in `receiving` by process termination or power loss. Projects carry `metadata.inbox_intake_id`, so the save-project -> receipt-link crash window is recoverable: an already-committed project is rediscovered instead of duplicated. File preparation resumes from immutable content-addressed bytes; URL/note intake restores or creates its project and finishes. A receipt interrupted before asset acceptance becomes explicitly failed rather than remaining permanently `receiving`.

## URL/note capture

URL/note intake deliberately does not create a fake `Asset` or fake `SourceRecord` when no bytes exist. It creates a durable Inbox intake plus an assetless `Project(INBOX)` whose metadata preserves the captured URL/note/creator hint. A future source provider can resolve bytes later without changing this boundary.

## Thumbnail derivative

PR8 writes a deterministic JPEG derivative under a runtime-relative key derived from source SHA-256 and thumbnail spec, then records it through the existing `DerivativeSlot` mechanism. Publication is atomic: FFmpeg writes a same-directory temporary JPEG and the final path appears only after a non-empty output is produced and fsynced. Reuse requires an existing derivative receipt whose canonical key, source digest, and output digest match the bytes. Serving repeats those checks and fails closed on tampering.

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
