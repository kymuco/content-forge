# PR9 PWA and Share-to-Inbox contract

PR9 adds the first phone-facing client above the PR8 authenticated local API. It does not move Inbox, storage, probe, thumbnail, project, or render semantics into JavaScript.

```text
Android Share / file picker / URL note
                |
        installed /app/ PWA
                |
      IndexedDB retry queue
                |
       authenticated PR8 API
                |
             Inbox
```

## PWA shell

The shell is packaged inside the Python distribution and served from `/app/`; no Node or external CDN/runtime is required. The manifest uses relative `id`, `start_url`, `scope`, icon, and share-target URLs so the same shell remains valid when the ASGI app is mounted below a `root_path`.

The shell provides:

- install prompt support;
- bearer pairing and confirmed revocation;
- mobile file capture with XHR upload progress;
- URL/note capture;
- recent Inbox cards with authenticated thumbnail fetches;
- persistent retry queue in IndexedDB;
- desktop-only loopback onboarding for a phone-visible HTTPS address and pairing QR.

Static UI responses use a restrictive Content Security Policy, no-referrer policy, `nosniff`, and frame denial. The client renders intake-controlled strings with `textContent`; it does not inject Inbox content as HTML.

The server exposes a generated same-origin `/app/config.js` before `shared.js`. It carries the configured upload, multipart-body, and browser-queue limits into both the page and Service Worker at load/update time. Controlled `config.js` GETs are network-first with `cache: no-store`; a successful live response refreshes the scope-specific cache, while the cached copy is only an offline fallback. Service Worker registration uses `updateViaCache: "none"`, so update checks fetch both the worker script and its imported configuration without letting the HTTP cache hide a changed authority.

The server additionally exposes `/app/config.json` with the same limit payload and `Cache-Control: no-store`. An already-active worker fetches and validates this live authority immediately before an online Android share. A server-side limit change therefore applies to the next reachable share without waiting for a worker update cycle. A network failure either before response headers or while reading the response body permits fallback to the worker's last validated frozen limits so genuine offline capture remains possible. Once a complete response body has been received, malformed JSON/configuration and non-success HTTP responses are not treated as offline conditions and fail closed.

Shell caches use a Content Forge + Service Worker scope-specific prefix. Worker activation therefore removes only obsolete caches for the same mounted Content Forge instance, not caches owned by another application or mount on the origin. Navigation requests fall back to the cached `/app/` shell when the server is offline, including redirected share-marker URLs such as `?shared=1`.

IndexedDB is origin-scoped by browsers, so PR9 additionally includes the mounted `/app/` scope path in the database namespace. Two Content Forge instances mounted on one reverse-proxy origin therefore do not share bearer tokens, queued captures, queue deletion, or drain state.

Foreground capture treats persistence as the acceptance boundary. File-picker selections are not cleared until the complete batch has been atomically queued, and URL/note form values are not cleared until their record is queued. Quota or IndexedDB failures are surfaced in the capture UI and leave the user's current selection/form values available rather than becoming silent unhandled promise rejections. If queue refresh/drain fails after persistence, the UI explicitly reports that the capture is already preserved locally.

## Android Web Share Target

The manifest declares a multipart Web Share Target for image/video/audio files plus title/text/URL metadata.

A correctly installed Service Worker intercepts `POST app/share-target` inside the browser. Before consuming the body it requires the expected navigation/document Fetch Metadata provenance, rejects cross-site provenance, requires a previously paired local session token, validates multipart Content-Type, resolves the current online server limits when reachable, and rejects an advertised oversized Content-Length when that header is visible. Browser Web Share Target FetchEvent requests are not required to expose that network-generated header.

The actual pre-parser bound is the request stream itself. The worker wraps `request.body` in a bounded `ReadableStream`, counts bytes against the active `maxShareBodyBytes`, and cancels/rejects before enqueueing any chunk that would cross the cap. That bounded stream feeds the browser multipart parser directly: the worker does not accumulate all chunks and does not construct a second full-size `Blob`, so peak JavaScript ownership remains proportional to stream buffering rather than multiple complete payload copies. The raw FetchEvent request is never handed directly to the multipart parser.

After parsing, the worker permits only the declared share fields and applies the freshly resolved per-file, filename/MIME/text, batch-count, queue-entry-count, and aggregate queued-byte limits before persistence. `shared.js` remains the final shared persistence boundary for Android Share, the foreground file picker, and URL/note capture. For an online Android share the worker passes the freshly validated live limit snapshot into that same atomic persistence operation, so both increases and decreases in server authority govern normalization and quota validation all the way through the IndexedDB commit. When the server is genuinely unreachable, including an interrupted live-config body stream, the worker uses the frozen previously validated limits for offline capture. Foreground page producers continue to use their loaded local configuration until normal page refresh/update.

Multi-file Android shares and multi-file foreground picker selections are each submitted as one atomic batch. Validation happens against the existing queue and every record is inserted in the same IndexedDB read/write transaction; quota exhaustion, duplicate IDs, or another IndexedDB failure cannot leave an already-committed prefix that would be duplicated when the user retries.

A share can still be captured while the Content Forge server is temporarily offline, but an unpaired browser profile cannot use the share target as an unauthenticated IndexedDB intake surface. The Service Worker stores accepted File/URL/note material in IndexedDB and redirects to the PWA shell. It does **not** call storage or Inbox internals and it does not attach the bearer token to the OS share-target navigation itself.

If the Service Worker is not active, the server-side `POST /app/share-target` fallback never parses or ingests the multipart body. Because this request has reached the HTTP server rather than a FetchEvent, the fallback requires and bounds Content-Length before returning guidance to open/install the PWA first. This prevents the fallback surface from becoming an authentication bypass around PR8.

## Authenticated queue drain and idempotency

The foreground shell drains queued entries only through the existing authenticated PR8 endpoints:

- files -> `POST api/v1/inbox/files`;
- URL/note -> `POST api/v1/inbox/url-note`.

Every IndexedDB queue record has a stable UUID. PR9 sends that UUID as `Idempotency-Key` on each authenticated capture attempt. The API validates the key and deterministically derives the intake primary key from it. The intake identity is therefore committed atomically with the initial SQLite receipt rather than being allocated afresh on every HTTP call.

This closes the ambiguous-response window: if the server durably accepted an operation but the network connection disappeared before the browser received the success response, retrying the same queue record resolves to the same intake/project lineage instead of creating a duplicate. Reusing one idempotency key for different immutable URL/note or file metadata is rejected with `409`.

File idempotency also binds accepted content, not just the filename and MIME metadata. Once the durable byte receipt contains exact size + SHA-256, a replay is hashed from the already-spooled authenticated upload and must match that receipt. Reusing the key with different file bytes therefore returns `409` rather than falsely replaying the old project as acceptance of new content.

FastAPI may run synchronous capture handlers concurrently even though one process exclusively owns the Content Forge root. PR9 therefore serializes live side-effect execution through a fixed set of idempotency-key lock stripes. Two simultaneous requests for one queue UUID cannot both observe and advance the same `receiving` receipt; sequential retries retain the durable SQLite identity and recovery semantics without an unbounded in-memory per-key lock map.

For files, a sequential retry before the durable byte-acceptance receipt reuses the same deterministic intake. A failed FILE receipt with no durable content SHA, asset, or project has accepted no bytes and is therefore revivable under that same identity regardless of its previous pre-acceptance error. The retry re-evaluates the request against **current** runtime authority. Operational failures such as `PermissionError`, `FileNotFoundError`, platform-specific `OSError` subclasses, SQLite errors, and interrupted startup reconciliation can recover normally. Contextual validation is re-evaluated too: an `UploadTooLargeError` returns `413` again while the same limit remains in force, but a later deliberate increase in `max_upload_bytes` can accept the same queued bytes under the original idempotent intake identity.

Once exact size + SHA-256 are durable, a FILE intake that is still `receiving` is an accepted recovery checkpoint rather than a terminal replay. A same-key retry must hash and count the authenticated retry body against that durable receipt; different bytes return `409`, while an exact match resumes the existing asset/source/project/preparation lineage without re-accepting content or waiting for a server restart. Prepared/partial receipts remain terminal replay results, and a post-acceptance durable `failed` intake is not converted into synthetic success; ordinary terminal failed receipts replay as `409` and remain inspectable in Inbox. Existing PR8 clients that omit `Idempotency-Key` retain the original non-idempotent API behavior; the PR9 PWA always supplies it for queued captures.

Application-layer recovery checkpoints are deliberately distinct from transport success. `InboxService.ingest_upload()` may return a durable accepted `RECEIVING/post_acceptance_retryable` FILE checkpoint to local recovery callers, but `POST /api/v1/inbox/files` never serializes that incomplete result as `201`: it returns retryable `500` until an exact-byte same-key retry completes handoff. URL/note capture follows the same transport rule without a byte-acceptance receipt: transient project/storage failures may leave a deterministic `RECEIVING/capture_retryable` intake, and `POST /api/v1/inbox/url-note` returns retryable `500` until the same-key retry completes the existing project lineage. In both cases the PWA therefore keeps its only IndexedDB copy instead of deleting it as a false success.

Queue failure handling distinguishes retryable, preserved, and permanently rejected failures. Network failures, 5xx, `408`, `425`, and `429` preserve the current queue item and stop the drain for later retry. `401` invalidation is scoped to the exact bearer that authorized the request: a late response from token A cannot clear or overwrite a newer token B installed after re-pairing. If the old response arrives while B is current, the queued record is preserved and retried under B after the old drain releases its lock. Deterministic permanent 4xx responses such as true idempotency `409` conflicts remove the rejected record and continue with later captures so a poison item cannot block FIFO forever. `413` is intentionally non-destructive because it can reflect upload authority that changed after local capture; the item remains queued and later entries continue. On a later retry the server re-evaluates an unaccepted oversized receipt against the current limit rather than replaying an obsolete contextual failure forever.

## Pairing, revocation, and QR onboarding

The bearer token is stored in the mount-scoped IndexedDB database so the page and Service Worker for one PWA instance can share one persistent paired-device state without leaking that state to another Content Forge mount on the same origin. A normally paired bearer is promoted only after IndexedDB persistence succeeds. If persistence fails **after** the server has already issued a session, however, the only issued token is immediately exposed as the in-memory current bearer and the paired/Disconnect UI is shown before automatic cleanup begins. Automatic revocation is then attempted using that exact token. This means even a stalled cleanup request cannot trap the sole revocation credential inside an async closure; the user retains a visible Disconnect path while cleanup is pending. Confirmed automatic revocation clears that same bearer if it is still current, while an unconfirmed cleanup deliberately leaves it in memory for manual revocation.

Persistent pairing ownership is compare-and-set across tabs. `setToken()` uses one mount-scoped IndexedDB read/write transaction to inspect and claim the token slot: an empty slot accepts the issued bearer, the same bearer is idempotent, and a different existing bearer aborts the claim instead of overwriting it. A losing exchange exposes and revokes only its own issued bearer; compare-and-delete cleanup cannot erase the persisted winner. After confirmed loser cleanup, the tab rereads the token slot and immediately adopts the surviving winner, restoring paired state and normal queue/Inbox flow without requiring a reload. Two concurrent pairing exchanges therefore cannot silently orphan one another's server sessions by last-writer-wins persistence.

Every authenticated foreground request captures the bearer that actually authorized it. Server revocation and `401` responses may invalidate local state only if that captured bearer still equals the current bearer. IndexedDB cleanup is additionally compare-and-delete: the token key is removed in one read/write transaction only when its stored value still matches the invalidated bearer. Therefore a slow request or cleanup begun under token A cannot erase a newer token B even if B is written while the old asynchronous operation is still completing.

Normal revocation separates server authority from local cleanup. A network failure or a non-success response other than `401` for the current bearer leaves that bearer and paired UI intact so revocation can be retried. Once `DELETE /api/v1/sessions/current` succeeds, or `401` proves that same current credential is already unusable, the in-memory bearer is cleared and the UI transitions to disconnected state before compare-and-delete IndexedDB cleanup is awaited. If local deletion fails, the client reports a stale-token cleanup problem but does not falsely claim that the server session remains live. A later reload that encounters the stale invalid bearer follows the same bearer-scoped `401` invalidation path instead of remaining wedged in paired state.

A second QR cannot silently replace an already stored bearer. The fragment secret is removed from visible history immediately, but when the installation is already paired the client refuses the new exchange and requires an explicit Disconnect first. The previous server session therefore remains revocable instead of becoming an orphaned credential that only expires later.

Pairing authority remains PR8's authority: challenge creation is still restricted to loopback socket peer + loopback Host + loopback Origin when present.

For desktop onboarding, the loopback shell supplies the phone-visible base URL. PR9 accepts:

- HTTPS for LAN/non-loopback addresses;
- HTTP only for loopback addresses;
- no userinfo, query, fragment, or path traversal.

The generated QR points to:

```text
<phone-visible-base>/app/#challenge_id=...&code=...
```

The short-lived pairing code is intentionally in the URL **fragment**, not the query or path. Browser fragments are not included in the HTTP request or Referer. On load the PWA reads the fragment, immediately removes it from visible history state, and exchanges it only when no bearer is already installed; a successful exchange persists only the resulting session token locally.

QR rendering is local via the pure-Python `segno` package; no third-party QR service receives the address, challenge, or code.

## Review status

PR9 uses repeated independent Codex passes as an adversarial correctness/security gate. Historical findings and their resolutions are tracked in the pull-request review threads rather than frozen as pass/finding counts in this contract. Every actionable finding must be fixed or shown already resolved by landed behavior, covered by a targeted regression where applicable, replied to, and resolved. The merge gate requires a green exact-head CI run, zero unresolved review threads, and a fresh review of the same immutable candidate with no actionable findings.

## Boundaries / non-goals

PR9 does not add:

- proxy preview or the PR10 review queue;
- project editing semantics;
- automatic URL downloading;
- publishing;
- a worker pool;
- direct FFmpeg/render invocation from the browser;
- public-internet hosting.

The normal target flow after one-time pairing/install is:

```text
Android Share -> scoped, live-authority-bounded, atomic local queue
              -> authenticated idempotent Inbox intake
              -> one project lineage
```
