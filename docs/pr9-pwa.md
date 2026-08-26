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
- bearer pairing and revocation;
- mobile file capture with XHR upload progress;
- URL/note capture;
- recent Inbox cards with authenticated thumbnail fetches;
- persistent retry queue in IndexedDB;
- desktop-only loopback onboarding for a phone-visible HTTPS address and pairing QR.

Static UI responses use a restrictive Content Security Policy, no-referrer policy, `nosniff`, and frame denial. The client renders intake-controlled strings with `textContent`; it does not inject Inbox content as HTML.

## Android Web Share Target

The manifest declares a multipart Web Share Target for image/video/audio files plus title/text/URL metadata.

A correctly installed Service Worker intercepts `POST app/share-target` inside the browser, parses the OS-provided multipart payload there, stores File/URL/note material in IndexedDB, and redirects to the PWA shell. The Service Worker does **not** call storage or Inbox internals.

The foreground shell drains queued entries only through the existing authenticated PR8 endpoints:

- files -> `POST api/v1/inbox/files`;
- URL/note -> `POST api/v1/inbox/url-note`.

The bearer token is stored in IndexedDB so both the page and Service Worker-owned queue can share one persistent client store; the Service Worker never attaches the token to the unauthenticated OS share-target navigation itself.

If the Service Worker is not active, the server-side `POST /app/share-target` fallback never parses or ingests the multipart body. It enforces a Content-Length bound and returns guidance to open/install the PWA first. This prevents the share-target surface from becoming an authentication bypass around PR8.

Queue entries are removed only after the authenticated API reports successful acceptance. Network failures, 5xx responses, or an expired session leave the entry queued for explicit or later retry. A 401 clears the local bearer token but preserves queued shares.

## Pairing and QR onboarding

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
Android Share -> Content Forge -> persistent local queue -> authenticated Inbox -> project card
```
