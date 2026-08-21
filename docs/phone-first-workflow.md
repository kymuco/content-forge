# Phone-first workflow

## Goal

The phone is the primary discovery device. Content Forge should make the normal path from finding a source to having it safely available on the desktop require only a few taps and no USB data connection.

The desktop remains the source of truth and compute worker. The phone is a thin, high-value control surface for ingest, review, and status.

## Default topology

At home/on the same network:

```text
Android phone
    |
   Wi-Fi
    |
Content Forge FastAPI/PWA on desktop
    |
local storage + worker + FFmpeg/TTS
```

The first version should use local network access only. The service must not bind a publicly reachable internet endpoint by default.

Later, out-of-home access can use a private overlay network such as Tailscale without redesigning the application protocol.

## PWA before native Android app

A PWA is the preferred first client because it provides:

- fast iteration;
- one UI codebase for phone and desktop;
- installable home-screen experience;
- file picker/camera/gallery access where supported;
- Android share-target support where browser/platform behavior permits;
- no app-store distribution requirement.

A minimal native Android wrapper remains an option if platform share behavior proves unreliable.

## Core mobile flows

### Flow 1 — Share media/files

Desired interaction:

```text
Browser / Reddit / Gallery / Files
-> Share
-> Content Forge
-> optional quick note/tags
-> Send to Inbox
```

The desktop receives the bytes, hashes them, stores them, extracts metadata, generates thumbnails/proxies, and creates or attaches them to a draft project.

### Flow 2 — Paste a URL

Sometimes the phone cannot or should not directly send the original media file. The PWA should support:

```text
+ Add
  [Upload files]
  [Paste URL]
  [Add note]
```

A URL intake record stores the source URL immediately. A future source provider may resolve/download it where appropriate; v0.1 does not require arbitrary downloading.

This distinction is important: source capture should not fail just because automated fetching is unavailable.

### Flow 3 — Send an image pack

For art/manga/manhwa workflows:

```text
select N images
-> Share/Upload
-> preserve selection order when available
-> create image-sequence draft
-> show reorder review card
```

The system should avoid requiring the user to rename files `01`, `02`, `03` manually.

### Flow 4 — Quick project creation

After ingest, the PWA may show:

```text
10 images received
Suggested kind: manga_panels
Suggested workflow: panel_sequence

[Use suggestion]
[Change]
[Later]
```

Suggestions can be deterministic or LLM-assisted, but skipping them must remain possible.

### Flow 5 — Review queue

The phone should excel at small decisions:

- choose/edit hook;
- confirm crop;
- reorder images;
- choose reaction asset;
- choose music preset;
- confirm creator credit;
- correct OCR later;
- assign speaker later;
- listen to/regenerate TTS line later;
- approve final preview.

Each task should fit on one focused screen/card whenever practical.

## Desktop-only or desktop-preferred work

The mobile-first goal does not mean forcing every operation onto a small screen.

Desktop remains preferable for:

- unusual freeform editing;
- inspecting many sources simultaneously;
- creating/revising templates/skins;
- debugging render failures;
- detailed audio work;
- development/configuration.

The UX rule is simply: a routine two-second decision should not require the desktop.

## Discovery-to-desktop latency

The system should feel immediate on a home network.

A useful target for ordinary files:

```text
share accepted on phone
-> upload starts immediately
-> asset card visible as soon as server creates record
-> metadata/thumbnail fill in asynchronously
```

Large files should not block the UI. Upload progress and resumable/retry behavior should be added as complexity warrants.

## Local addressing and onboarding

The desktop UI should display:

- local URL;
- QR code;
- server status;
- storage root/status;
- worker status.

A friendly local hostname such as `content-forge.local` is desirable through mDNS where reliable, with the LAN IP as fallback.

The first connection flow should be closer to:

```text
Start Content Forge
-> scan QR
-> install PWA
```

than to manually copying IP addresses every session.

## Authentication/security

Even on a LAN, write operations should not assume every device is trusted.

The initial design should support a simple pairing/session mechanism rather than an unauthenticated upload endpoint bound to all interfaces.

Possible v0.1 approach:

1. desktop generates a short-lived pairing token/QR;
2. phone pairs once;
3. server issues a local application token/session;
4. write/review endpoints require it;
5. tokens can be revoked from desktop settings.

Do not expose provider cookies, local file-system paths, or arbitrary command execution through the mobile API.

## Share payload model

A mobile ingest request may carry:

```text
files[]
source_url (optional)
note (optional)
creator/artist hint (optional)
content kind hint (optional)
project target (new/existing)
```

The server owns canonical hashing and metadata extraction.

## Mobile preview

Final 1080x1920 rendering is unnecessary for every edit round.

The phone should receive a lightweight preview, for example:

```text
540x960
low bitrate
speed-oriented encode
```

The same normalized timeline is used for preview and final render to avoid visual drift.

## Offline/desktop-unavailable behavior

A pure PWA on a phone cannot reliably deliver files to an offline desktop. v0.1 can fail clearly and preserve the user's local browser state only where easy.

A later companion/wrapper could support a local outbound queue, but this should not complicate the first useful version.

When the desktop is reachable, reliability matters more than elaborate offline semantics.

## Out-of-home later

The preferred future design is:

```text
Phone
  |
private overlay network (e.g. Tailscale)
  |
Content Forge desktop service
```

rather than port forwarding the service to the public internet.

The application should therefore avoid assuming a particular LAN subnet, but remote access is not a v0.1 dependency.

## Success criteria

Phone-first ingest/review is successful when all of the following are true:

1. A video or image pack found on Android can reach the desktop library without USB.
2. Source URL/creator notes can be captured at ingest time before they are forgotten.
3. The user can see upload/preparation status from the phone.
4. Common review tasks can be completed comfortably one-handed.
5. Preview approval and final render can be triggered from phone.
6. No credentials or raw storage tree need to be exposed to the phone.
