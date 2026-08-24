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

One live API process owns a runtime root at a time. `create_app()` acquires an OS advisory lock before startup reconciliation; a second live process using the same root fails closed instead of interpreting the first process's active `receiving` uploads as interrupted work. The operating system releases ownership when the process/file descriptor disappears, including crashes, so a later process can immediately acquire the root and run recovery. This is an enforced single-owner application boundary, not a general worker-pool lease protocol.

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
-> create durable intake receipt with reserved provenance ID
-> bounded application staging
-> flush + fsync staging file
-> fsync containing staging directory where supported
-> freeze exact size + SHA-256 in the intake receipt   [byte acceptance]
-> existing AssetStore bytes ingest + SHA-256 deduplication
-> durable asset linkage checkpoint
-> idempotent source/provenance record
-> deterministic Project(state=INBOX)
-> ffprobe
-> authoritative media classification + metadata enrichment
-> thumbnail derivative for visual media
-> terminal intake state
```

FastAPI/Starlette would normally parse/spool `UploadFile` before endpoint dependencies. PR8 therefore guards the upload route in middleware before multipart parsing: authentication is checked first, `Content-Length` is mandatory, and the complete multipart body is capped before Starlette consumes it. The application staging copy independently enforces the actual file-byte limit.

The application computes SHA-256 while copying the bounded upload. The complete staging file is flushed and fsynced; on POSIX, the containing incoming directory is also fsynced so the newly created filename is durable across the acceptance boundary. Only then are the exact byte count and SHA-256 committed to the intake receipt. That receipt transition is the byte-acceptance linearization point: before it, interruption may safely fail the intake and discard staging; after it, recovery must preserve or reproduce exactly those accepted bytes.

After byte acceptance but before a durable project checkpoint, transient filesystem/storage failures such as exhausted disk space do not terminally fail the intake. The receipt remains `receiving`; if no verified canonical/catalog Asset exists yet, the authenticated staging file is retained as the recovery authority. A later exclusive startup retries the handoff. Integrity failures are different: an ambiguous staging set, a size/digest mismatch, inconsistent linkage, or another non-transient recovery contradiction fails closed instead of retrying forever.

Provenance receives a stable source ID in the initial intake receipt. AssetStore is used for immutable bytes/deduplication without creating provenance inside the same opaque call; after the asset checkpoint, the reserved provenance record and Inbox project are attached idempotently. This ordering makes the post-acceptance cross-store handoffs discoverable and resumable.

Client-provided multipart MIME type and filename remain provenance/UI hints only. New uploaded bytes enter shared storage with neutral `OTHER` / `application/octet-stream` classification. A successful ffprobe is authoritative for media-derived metadata and may also repair legacy/deduplicated Asset classification that originated from older filename/MIME heuristics. Byte identity remains immutable: `asset_id`, SHA-256, size, storage key, and creation identity cannot be changed by enrichment.

Embedded artwork is not timeline video. ffprobe streams marked `disposition.attached_pic` are ignored when determining actual video presence, so an MP3/M4A with album art remains audio instead of becoming a video asset with cover-art dimensions.

A probe or thumbnail failure does not delete an already accepted immutable asset. The intake becomes `partial`, retains the project/asset identity, and records a path-safe diagnostic. Unexpected handled failures preserve durable links and fail closed without publishing raw subprocess/storage exception text.

## Interruption recovery

Application startup reconciles an intake left in `receiving` by process termination or power loss only after obtaining exclusive ownership of the runtime root. A file is considered accepted for interruption recovery only if its intake contains both the frozen SHA-256 and exact byte count.

After that acceptance point, recovery can resume from progressively later durable representations:

1. an already-linked verified Asset;
2. an Asset catalog row found by the frozen SHA-256;
3. the canonical content-addressed blob, if its expected path, exact size, and SHA-256 all verify;
4. the surviving application staging file, but only when exactly one file belongs to that intake and its exact size and SHA-256 match the frozen receipt.

A verified staging file is re-ingested through the existing AssetStore rather than promoted by path manipulation. If more than one staging file claims the intake, or its size/digest disagrees with the frozen receipt, recovery fails closed and does not accept those bytes. If AssetStore publication is temporarily unavailable, for example because storage is still full, the accepted receipt and authenticated staging file remain resumable rather than being destroyed. Once a canonical/catalog Asset is established and verified, obsolete staging for that intake is discarded.

If the process died after the canonical content-addressed blob was atomically published but before its `assets` row committed, recovery reconstructs only the neutral Asset metadata row after verifying the canonical blob against the frozen receipt. It then restores the reserved SourceRecord, project linkage, and media preparation idempotently.

Project identity is deterministically derived from the intake's UUID payload and project timestamps are frozen to intake creation time. Recovery of an accepted unlinked intake therefore converges on the same canonical project ID and manifest instead of allocating duplicate random projects. SQLite receipt transitions remain serialized/CAS-checked. Pre-hardening PR8 receipts with an older randomly allocated project remain discoverable through `metadata.inbox_intake_id`.

Projects carry `metadata.inbox_intake_id`, so the save-project -> receipt-link crash window is recoverable: an already-committed project is rediscovered instead of duplicated. URL/note intake restores or creates its project and finishes. A file receipt with no complete frozen byte identity, or with no verified recoverable representation after acceptance, becomes explicitly failed rather than remaining permanently `receiving`.

## URL/note capture

URL/note intake deliberately does not create a fake `Asset` or fake `SourceRecord` when no bytes exist. It creates a durable Inbox intake plus an assetless `Project(INBOX)` whose metadata preserves the captured URL/note/creator hint. A future source provider can resolve bytes later without changing this boundary.

## Thumbnail derivative

PR8 writes a deterministic JPEG derivative under a runtime-relative key derived from source SHA-256 and thumbnail spec, then records it through the existing `DerivativeSlot` mechanism. Publication is atomic: FFmpeg writes a same-directory temporary JPEG and the final path appears only after a non-empty output is produced and fsynced. Process-local publication for a canonical thumbnail key is serialized; because the runtime root has exactly one live API owner, concurrent requests for identical bytes converge on one publication rather than deleting or replacing each other's output. Reuse requires an existing derivative receipt whose canonical key, source digest, and output digest match the bytes. Serving repeats those checks and fails closed on tampering.

## Explicit non-goals

PR8 does not add:

- a PWA or mobile UI;
- a public-internet deployment mode;
- a worker pool or batch scheduler;
- a general multi-process worker/lease protocol beyond enforced single-owner API runtime roots;
- automatic source downloading;
- direct render execution from HTTP handlers;
- review-task UI;
- publishing.

Those remain later roadmap layers over this application boundary.
