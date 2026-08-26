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

The server additionally exposes `/app/config.json` with the same limit payload and `Cache-Control: no-store`. An already-active worker fetches and validates this live authority immediately before an online Android share. A server-side limit change therefore applies to the next reachable share without waiting for a worker update cycle. Only an actual network exception permits fallback to the worker's last validated frozen limits so genuine offline capture remains possible; HTTP errors or malformed live configuration fail closed.

Shell caches use a Content Forge + Service Worker scope-specific prefix. Worker activation therefore removes only obsolete caches for the same mounted Content Forge instance, not caches owned by another application or mount on the origin. Navigation requests fall back to the cached `/app/` shell when the server is offline, including redirected share-marker URLs such as `?shared=1`.

IndexedDB is origin-scoped by browsers, so PR9 additionally includes the mounted `/app/` scope path in the database namespace. Two Content Forge instances mounted on one reverse-proxy origin therefore do not share bearer tokens, queued captures, queue deletion, or drain state.

## Android Web Share Target

The manifest declares a multipart Web Share Target for image/video/audio files plus title/text/URL metadata.

A correctly installed Service Worker intercepts `POST app/share-target` inside the browser. Before consuming the body it requires the expected navigation/document Fetch Metadata provenance, rejects cross-site provenance, requires a previously paired local session token, validates multipart Content-Type, resolves the current online server limits when reachable, and rejects an advertised oversized Content-Length when that header is visible. Browser Web Share Target FetchEvent requests are not required to expose that network-generated header.

The actual pre-parser bound is the request stream itself. The worker wraps `request.body` in a bounded `ReadableStream`, counts bytes against the active `maxShareBodyBytes`, and cancels/rejects before enqueueing any chunk that would cross the cap. That bounded stream feeds the browser multipart parser directly: the worker does not accumulate all chunks and does not construct a second full-size `Blob`, so peak JavaScript ownership remains proportional to stream buffering rather than multiple complete payload copies. The raw FetchEvent request is never handed directly to the multipart parser.

After parsing, the worker permits only the declared share fields and applies the freshly resolved per-file, filename/MIME/text, batch-count, queue-entry-count, and aggregate queued-byte limits before persistence. `shared.js` remains the final shared persistence boundary for Android Share, the foreground file picker, and URL/note capture. For an online Android share the worker passes the freshly validated live limit snapshot into that same atomic persistence operation, so both increases and decreases in server authority govern normalization and quota validation all the way through the IndexedDB commit. When the server is genuinely unreachable, the worker uses the frozen previously validated limits for offline capture. Foreground page producers continue to use their loaded local configuration until normal page refresh/update.

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

For files, a sequential retry before the durable byte-acceptance receipt reuses the same deterministic intake. A failed deterministic FILE receipt with no durable content SHA, asset, or project has accepted no bytes, so it is retryable by default under that same identity regardless of the concrete operational exception class (`PermissionError`, `FileNotFoundError`, platform-specific `OSError` subclasses, SQLite storage errors, or an interrupted startup reconciliation). Only explicitly deterministic pre-acceptance failures are terminal; `UploadTooLargeError` is currently the terminal input failure in this boundary.

Once exact size + SHA-256 are durable, or the intake is already prepared/partial, the existing durable result is replayed rather than executing a second lineage. A durable permanent `failed` intake is not converted into a synthetic successful replay. Ordinary terminal failed receipts replay as `409` and remain inspectable in Inbox. `UploadTooLargeError` is deliberately preserved as `413` on every same-key replay rather than being collapsed to `409`, because the PWA's `413` path is non-destructive and must not delete the only local copy merely because a retry reached an already-recorded oversized failure. Existing PR8 clients that omit `Idempotency-Key` retain the original non-idempotent API behavior; the PR9 PWA always supplies it for queued captures.

Queue failure handling distinguishes retryable, preserved, and permanently rejected failures. Network failures, 5xx, `408`, `425`, and `429` preserve the current queue item and stop the drain for later retry. `401` clears the expired local session token while preserving queued shares for re-pairing. Deterministic permanent 4xx responses such as true idempotency `409` conflicts remove the rejected record and continue with later captures so a poison item cannot block FIFO forever. `413` is intentionally special: because it can reveal a changed server upload authority relative to an already-captured local item, including a replay of the same durable `UploadTooLargeError`, the PWA preserves that item's only local bytes, surfaces guidance to refresh the current limits, and continues attempting later queue records rather than deleting the capture.

## Pairing, revocation, and QR onboarding

The bearer token is stored in the mount-scoped IndexedDB database so the page and Service Worker for one PWA instance can share one persistent paired-device state without leaking that state to another Content Forge mount on the same origin. Revocation is confirmation-sensitive: the client clears its local token only after `DELETE /api/v1/sessions/current` succeeds. A network failure or non-success response retains the token locally so the security-sensitive revocation can be retried instead of silently presenting a disconnected state while the server session remains live.

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

Five independent Codex review passes have produced 22 actionable correctness/security findings in total (8 P1 and 14 P2). Every finding now has a code fix or an already-landed equivalent fix and a targeted regression where applicable. The release gate still requires exact-head CI and a fresh review of the final immutable candidate rather than treating resolved historical findings as proof by themselves.

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
