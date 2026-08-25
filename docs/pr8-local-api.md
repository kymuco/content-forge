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

Plain HTTP is supported only for loopback development/use. Any non-loopback bind, including `--lan`, requires a TLS certificate and private key; the server refuses to start otherwise. The application request boundary independently rejects non-loopback plaintext HTTP, so serving the public `create_app()` factory programmatically through Uvicorn or another ASGI host cannot bypass this transport guarantee. The certificate must be trusted by the phone and valid for the address used by the client.

Only narrow bootstrap operations are unauthenticated:

- `GET /health`
- `POST /api/v1/pairing/exchange`

`POST /api/v1/pairing/challenges` is restricted to a loopback socket peer and also validates loopback `Host` plus browser `Origin` when one is present. This prevents a hostile browser origin or DNS-rebound hostname from using the loopback socket as proof of desktop user presence.

A challenge contains an opaque challenge ID plus an eight-digit short-lived code. The database stores a salted digest, not the plaintext code. Challenges are one-time and have a bounded failed-attempt budget.

Successful exchange returns a high-entropy bearer token. Only its SHA-256 digest is persisted. Sessions expire and can be revoked with `DELETE /api/v1/sessions/current`.

Parsed-body POST routes are bounded at the transport middleware before FastAPI/Pydantic body parsing. `POST /api/v1/pairing/exchange` requires a valid `Content-Length` and is capped at 128 KiB despite being intentionally unauthenticated. `POST /api/v1/inbox/url-note` is bearer-authenticated before JSON parsing and then receives the same 128 KiB body cap. File upload keeps its separate upload-sized multipart bound. All of these checks use the route-relative ASGI path so mounts/nonempty `root_path` do not bypass them.

One live API process owns a runtime root at a time. `create_app()` acquires an OS advisory lock before startup reconciliation; a second live process using the same root fails closed instead of interpreting the first process's active `receiving` uploads as interrupted work. The operating system releases ownership when the process/file descriptor disappears, including crashes, so a later process can immediately acquire the root and run recovery. This is an enforced single-owner application boundary, not a general worker-pool lease protocol.

The runtime-root directory tree is itself part of the durability boundary. On POSIX, before recursive creation `RuntimePaths.ensure()` records the nearest already-existing ancestor, creates the configured root and any missing parents, then fsyncs the new root directory chain through that pre-existing ancestor. This persists the configured root's own directory entry and any newly-created intermediate entries before later accepted-byte durability relies on files beneath that tree. Windows uses the strongest portable file durability primitives available because Python does not expose a portable directory-fsync equivalent.

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
durably establish runtime root where supported
-> pre-parse bearer authentication + Content-Length bound
-> create durable intake receipt with reserved provenance ID
-> bounded application staging
-> flush + fsync staging file
-> fsync containing staging directory where supported
-> fsync staging directory chain through runtime root where supported
-> FULL-synchronous SQLite commit of exact size + SHA-256   [byte acceptance]
-> AssetStore content-addressed ingest + SHA-256 deduplication
-> atomic canonical-blob publish or authenticated canonical reuse
-> fsync canonical blob directory chain where supported
-> durable asset linkage checkpoint
-> idempotent source/provenance record
-> deterministic Project(state=INBOX)
-> ffprobe
-> authoritative media classification + metadata enrichment
-> thumbnail derivative for visual media
-> terminal intake state
```

FastAPI/Starlette would normally parse/spool `UploadFile` before endpoint dependencies. PR8 therefore guards the upload route in middleware before multipart parsing: authentication is checked first, `Content-Length` is mandatory, and the complete multipart body is capped before Starlette consumes it. The middleware identifies the upload endpoint from the route-relative ASGI path rather than the externally prefixed URL path, so the same pre-parser barrier remains active when `create_app()` is mounted under another ASGI application or served with a nonempty `root_path`. The application staging copy independently enforces the actual file-byte limit. The same transport layer also requires valid `Content-Length` and a 128 KiB cap before parsing the pairing-exchange and URL/note JSON bodies; URL/note authentication occurs before its body is parsed.

The application computes SHA-256 while copying the bounded upload. The complete staging file is flushed and fsynced. On POSIX it first fsyncs the containing incoming directory and then fsyncs the directory chain through the already-durably-established runtime root, so both the new staging filename and newly-created directory entries such as `.incoming` survive the acceptance boundary. Only after those barriers succeed are the exact byte count and SHA-256 committed to the intake receipt using a dedicated SQLite transaction configured with `PRAGMA synchronous = FULL` before `BEGIN IMMEDIATE`. That FULL-synchronous receipt transition is the byte-acceptance linearization point: before it, interruption may safely fail the intake and discard staging; after it returns, recovery must preserve or reproduce exactly those accepted bytes even across process or machine power loss. Ordinary reconstructible application/catalog checkpoints remain on the database's normal WAL `synchronous=NORMAL` policy; FULL is reserved for this acceptance boundary rather than imposed on every write.

AssetStore publishes a new canonical content-addressed blob with an atomic rename. On POSIX it then fsyncs the canonical shard-directory chain through the established runtime root before a new asset catalog row can commit. A pre-existing canonical pathname is not treated as proof that an earlier directory-fsync completed: successful ingest/reuse reasserts the same directory-chain durability barrier, and authoritative recovery verification also reasserts it before canonical bytes are allowed to replace staging as the durable recovery authority. A committed or reused Asset therefore cannot intentionally outrun durability of the canonical blob directory entry. Windows uses the strongest portable file-fsync behavior available because Python does not expose a portable directory-fsync primitive there.

After byte acceptance, transient operational storage failures do not terminally fail the file intake merely because later asset, provenance, or project checkpoints already exist. This applies both during the live upload request and during startup reconciliation, and includes filesystem failures such as exhausted disk space or directory-sync `EIO`, plus SQLite operational failures such as a temporarily locked/busy catalog or database disk-pressure error. Thumbnail filesystem/publication failures are part of this same operational class: an `OSError` from thumbnail temp-file handling, fsync, atomic rename, directory durability, or derivative storage propagates to the outer retry policy instead of being converted into a terminal `thumbnail_failed` outcome. The receipt remains `receiving`; when canonical durability is not yet established, the authenticated staging file is retained as the recovery authority even if an asset/project link has already been checkpointed. If a cataloged and durable Asset already exists, staging is obsolete and need not be retained merely to make preparation retryable. A later exclusive startup retries the handoff/preparation. Integrity failures are different: an ambiguous staging set, a size/digest mismatch, inconsistent linkage, a present-but-corrupt canonical blob, or another non-transient recovery contradiction fails closed instead of retrying forever.

Failure handling is conservative when receipt authority itself is temporarily unreadable. If a live post-acceptance operation fails and the follow-up `get_intake()` used to classify cleanup also fails because of SQLite/storage pressure, the handler does not guess that staging is disposable: any staged file is retained and the original primary failure propagates. Exclusive startup reconciliation later reads the durable receipt and decides whether that retained staging is the accepted byte authority or merely pre-acceptance residue. This may temporarily preserve unnecessary staging, but it cannot delete potentially FULL-accepted bytes merely because the receipt could not be reread during the same failure episode.

Shutdown/control-flow signals are not application failures. `KeyboardInterrupt`, `SystemExit`, and other non-`Exception` signals propagate from live file ingest without terminalizing a FULL-accepted receipt; when no Asset receipt exists yet, its authenticated staging copy is preserved for startup recovery. URL/note capture likewise does not convert such a shutdown signal into a terminal intake failure.

Provenance receives a stable source ID in the initial intake receipt. AssetStore is used for immutable bytes/deduplication without creating provenance inside the same opaque call; after the asset checkpoint, the reserved provenance record and Inbox project are attached idempotently. This ordering makes the post-acceptance cross-store handoffs discoverable and resumable.

Client-provided multipart MIME type and filename remain provenance/UI hints only. New uploaded bytes enter shared storage with neutral `OTHER` / `application/octet-stream` classification. A successful ffprobe is authoritative for media-derived metadata and may also repair legacy/deduplicated Asset classification that originated from older filename/MIME heuristics. Byte identity remains immutable: `asset_id`, SHA-256, size, storage key, and creation identity cannot be changed by enrichment.

Embedded artwork is not timeline video. ffprobe streams marked `disposition.attached_pic` are ignored when determining actual video presence, so an MP3/M4A with album art remains audio instead of becoming a video asset with cover-art dimensions.

A media-content preparation failure does not delete an already accepted immutable asset. ffprobe failures and bounded FFmpeg thumbnail-generation outcomes such as nonzero exit, timeout, or missing/empty output produce a path-safe partial result while retaining the project/asset identity. Operational filesystem/SQLite failures are not folded into that `partial` category; after FULL acceptance they remain `receiving` and retryable under the storage policy above.

## Interruption recovery

Application startup reconciles an intake left in `receiving` by process termination or power loss only after obtaining exclusive ownership of the runtime root. A file is considered accepted for interruption recovery only if its intake contains both the frozen SHA-256 and exact byte count from the FULL-synchronous acceptance transition. `KeyboardInterrupt`, `SystemExit`, and other non-`Exception` control-flow signals propagate out of reconciliation without converting the intake to failure or deleting authenticated staging.

After that acceptance point, recovery can resume from progressively later durable representations:

1. an already-linked Asset whose canonical bytes verify and whose canonical directory durability barrier succeeds;
2. an Asset catalog row found by the frozen SHA-256 whose canonical bytes verify and whose canonical directory durability barrier succeeds;
3. the canonical content-addressed blob, if its expected path, exact size, and SHA-256 all verify and its directory chain is synced before reconstructing a missing catalog row;
4. the surviving application staging file, but only when exactly one file belongs to that intake and its exact size and SHA-256 match the frozen receipt.

A verified staging file is re-ingested through the existing AssetStore rather than promoted by path manipulation. If a catalog row exists but its canonical blob is missing, recovery may use exactly one authenticated surviving staging file to republish that same byte identity through AssetStore and requires convergence to the same catalog asset. This additionally repairs interrupted pre-hardening PR8 states from before canonical-directory durability was enforced. If no authenticated staging copy exists for a missing cataloged blob, recovery fails closed rather than retrying the contradiction indefinitely.

If more than one staging file claims the intake, or its size/digest disagrees with the frozen receipt, recovery fails closed and does not accept those bytes. If AssetStore, SQLite, thumbnail storage, or a required directory durability barrier is temporarily unavailable—for example because storage is still full, the catalog is temporarily locked, or a directory fsync returns `EIO`—the FULL-accepted receipt remains `receiving` and authenticated staging remains resumable where it is still the recovery authority. This remains true even when asset/provenance/project links were already checkpointed. Once a canonical/catalog Asset is byte-verified and its required directory durability barrier succeeds, obsolete staging is no longer authoritative and cleanup becomes best-effort: an `EACCES`, `EIO`, or similar failure while deleting that obsolete copy cannot overturn an already completed `prepared`/`partial` upload or change its durable asset/project identity. A later maintenance pass may remove such residue.

If the process died after the canonical content-addressed blob was atomically published but before its `assets` row committed, recovery reconstructs only the neutral Asset metadata row after verifying the canonical blob against the frozen receipt and re-establishing canonical directory-chain durability. It then restores the reserved SourceRecord, project linkage, and media preparation idempotently.

Project identity is deterministically derived from the intake's UUID payload and project timestamps are frozen to intake creation time. Recovery of an accepted unlinked intake therefore converges on the same canonical project ID and manifest instead of allocating duplicate random projects. SQLite receipt transitions remain serialized/CAS-checked. Pre-hardening PR8 receipts with an older randomly allocated project remain discoverable through `metadata.inbox_intake_id`.

Projects carry `metadata.inbox_intake_id`, so the save-project -> receipt-link crash window is recoverable: an already-committed project is rediscovered instead of duplicated. URL/note intake restores or creates its project and finishes. A file receipt with no complete frozen byte identity, or with no verified recoverable representation after acceptance, becomes explicitly failed rather than remaining permanently `receiving`.

## URL/note capture

URL/note intake deliberately does not create a fake `Asset` or fake `SourceRecord` when no bytes exist. It creates a durable Inbox intake plus an assetless `Project(INBOX)` whose metadata preserves the captured URL/note/creator hint. Its JSON body is bearer-authenticated before parsing, requires a valid `Content-Length`, and is capped at 128 KiB at the transport boundary. A future source provider can resolve bytes later without changing this boundary. Process shutdown signals propagate without converting an otherwise recoverable `receiving` URL/note intake into `failed`, so the next exclusive startup can restore or create the deterministic project and finish the receipt.

## Thumbnail derivative

PR8 writes a deterministic JPEG derivative under a runtime-relative key derived from source SHA-256 and thumbnail spec, then records it through the existing `DerivativeSlot` mechanism. Publication is receipt-safe: FFmpeg writes a same-directory temporary JPEG, the temporary file is fsynced, it is atomically renamed to the canonical path, and on POSIX the thumbnail shard-directory chain is fsynced through the established runtime root before the derivative-slot receipt is committed. A receipt therefore does not intentionally outrun durability of the canonical thumbnail directory entry. Process-local publication for a canonical thumbnail key is serialized; because the runtime root has exactly one live API owner, concurrent requests for identical bytes converge on one publication rather than deleting or replacing each other's output. Reuse requires an existing derivative receipt whose canonical key, source digest, and output digest match the bytes. Serving repeats those checks and fails closed on tampering.

Thumbnail outcome classification separates media-generation failure from storage failure. A bounded FFmpeg content outcome (nonzero exit, timeout, missing/empty output) becomes `thumbnail_failed` and may yield a terminal `partial` intake. Filesystem/storage `OSError` during thumbnail publication or durability is allowed to propagate unchanged so the FULL-accepted intake remains `receiving` and is retried after storage recovers.

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