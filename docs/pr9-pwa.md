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

The server also exposes a generated same-origin `/app/config.js` before `shared.js`. It carries the authoritative `max_upload_bytes`, multipart body budget, and browser queue limits into both the page and Service Worker. The worker therefore cannot silently accept a 2 GiB file when the local API was configured for a smaller maximum.

Shell caches use a Content Forge + Service Worker scope-specific prefix. Worker activation therefore removes only obsolete caches for the same mounted Content Forge instance, not caches owned by another application or mount on the origin. Navigation requests fall back to the cached `/app/` shell when the server is offline, including redirected share-marker URLs such as `?shared=1`.

IndexedDB is origin-scoped by browsers, so PR9 additionally includes the mounted `/app/` scope path in the database namespace. Two Content Forge instances mounted on one reverse-proxy origin therefore do not share bearer tokens, queued captures, queue deletion, or drain state.

## Android Web Share Target

The manifest declares a multipart Web Share Target for image/video/audio files plus title/text/URL metadata.

A correctly installed Service Worker intercepts `POST app/share-target` inside the browser. Before multipart parsing it requires the expected navigation/document Fetch Metadata provenance, rejects cross-site provenance, requires a previously paired local session token, validates multipart Content-Type, and requires a Content-Length bounded by the server-provided configuration. Only then may it call `request.formData()`.

After parsing, the worker permits only the declared share fields. Queue validation itself is owned by `shared.js`, not by individual producers: Android Share, the foreground file picker, and URL/note capture all pass through the same per-file size, filename/MIME/text length, batch-count, queue-entry-count, and aggregate queued-byte limits.

A multi-file Android share is committed through one IndexedDB read/write transaction. Validation happens against the existing queue and then every record is inserted in that same transaction; quota exhaustion or another IndexedDB failure cannot leave an already-committed prefix of the share that would be duplicated when the user retries.

A share can still be captured while the Content Forge server is temporarily offline, but an unpaired browser profile cannot use the share target as an unauthenticated IndexedDB intake surface. The Service Worker stores accepted File/URL/note material in IndexedDB and redirects to the PWA shell. It does **not** call storage or Inbox internals and it does not attach the bearer token to the OS share-target navigation itself.

If the Service Worker is not active, the server-side `POST /app/share-target` fallback never parses or ingests the multipart body. It enforces the same configured Content-Length authority and returns guidance to open/install the PWA first. This prevents the fallback surface from becoming an authentication bypass around PR8.

## Authenticated queue drain and idempotency

The foreground shell drains queued entries only through the existing authenticated PR8 endpoints:

- files -> `POST api/v1/inbox/files`;
- URL/note -> `POST api/v1/inbox/url-note`.

Every IndexedDB queue record has a stable UUID. PR9 sends that UUID as `Idempotency-Key` on each authenticated capture attempt. The API validates the key and deterministically derives the intake primary key from it. The intake identity is therefore committed atomically with the initial SQLite receipt rather than being allocated afresh on every HTTP call.

This closes the ambiguous-response window: if the server durably accepted an operation but the network connection disappeared before the browser received the success response, retrying the same queue record resolves to the same intake/project lineage instead of creating a duplicate. Reusing one idempotency key for different immutable URL/note or file metadata is rejected with `409`.

File idempotency also binds accepted content, not just the filename and MIME metadata. Once the durable byte receipt contains exact size + SHA-256, a replay is hashed from the already-spooled authenticated upload and must match that receipt. Reusing the key with different file bytes therefore returns `409` rather than falsely replaying the old project as acceptance of new content.

FastAPI may run synchronous capture handlers concurrently even though one process exclusively owns the Content Forge root. PR9 therefore serializes live side-effect execution through a fixed set of idempotency-key lock stripes. Two simultaneous requests for one queue UUID cannot both observe and advance the same `receiving` receipt; sequential retries retain the durable SQLite identity and recovery semantics without an unbounded in-memory per-key lock map.

For files, a sequential retry before the durable byte-acceptance receipt reuses the same deterministic intake. If startup observed that receipt after interruption but before any exact bytes were accepted, it may have classified it as `interrupted_before_asset_acceptance`; PR9 revives that specific retryable pre-acceptance state, clears only its transient diagnostics, and lets the authenticated retry supply bytes again under the same identity. Retryable pre-acceptance filesystem/SQLite interruption codes receive the same treatment. Permanent failures such as `UploadTooLargeError` remain terminal.

Once exact size + SHA-256 are durable, or the intake is already prepared/partial, the existing durable result is replayed rather than executing a second lineage. A durable permanent `failed` intake is not converted into a synthetic successful replay: the same key receives `409`, while the failed intake remains inspectable in Inbox. Existing PR8 clients that omit `Idempotency-Key` retain the original non-idempotent API behavior; the PR9 PWA always supplies it for queued captures.

Queue failure handling distinguishes retryable and permanent failures. Network failures, 5xx, `408`, `425`, and `429` preserve the queue head for retry. `401` clears the expired local session token while preserving queued shares for re-pairing. Permanent 4xx responses, including deterministic idempotency `409`, remove that rejected record and continue draining later valid captures, preventing one poison item from blocking FIFO forever.

## Pairing, revocation, and QR onboarding

The bearer token is stored in the mount-scoped IndexedDB database so the page and Service Worker for one PWA instance can share one persistent paired-device state without leaking that state to another Content Forge mount on the same origin. Revocation is confirmation-sensitive: the client clears its local token only after `DELETE /api/v1/sessions/current` succeeds. A network failure or non-success response retains the token locally so the security-sensitive revocation can be retried instead of silently presenting a disconnected state while the server session remains live.

Pairing authority remains PR8's authority: challenge creation is still restricted to loopback socket peer + loopback Host + loopback Origin when present.

For desktop onboarding, the loopback shell supplies the phone-visible base URL. PR9 accepts:

- HTTPS for LAN/non-loopback addresses;
- HTTP only for loopback addresses;
- no userinfo, query, fragment, or path traversal.

The generated QR points to:

```text
<phone-visible-base>/app/#challenge_id=...&code=...
```

The short-lived pairing code is intentionally in the URL **fragment**, not the query or path. Browser fragments are not included in the HTTP request or Referer. On load the PWA reads the fragment, immediately removes it from visible history state, exchanges the challenge for a bearer session, and persists only the resulting session token locally.

QR rendering is local via the pure-Python `segno` package; no third-party QR service receives the address, challenge, or code.

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
Android Share -> scoped, bounded, atomic local queue
              -> authenticated idempotent Inbox intake
              -> one project lineage
```
