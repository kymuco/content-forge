# PR28 — YouTube Data API publishing adapter

## Goal

Turn the PR27 platform-agnostic publishing boundary into one real remote publishing path without making YouTube, Google OAuth, or network availability part of rendering/export correctness.

PR28 adds a YouTube Data API v3 adapter only. It does not add analytics, a second platform adapter, or an automatic scheduler daemon.

## Authority chain

`final RenderArtifactManifest`
→ authenticated artifact loader
→ exact credential-free `PublishRequest`
→ explicit human approval
→ durable `prepared` attempt
→ provider health/channel verification
→ side-effect-free YouTube preflight/read checks
→ authenticated immutable upload snapshot
→ prebuilt resumable `videos.insert` request
→ durable `running` remote boundary
→ `next_chunk()` remote upload
→ bounded processing verification
→ exact remote metadata/status verification
→ authenticated `PublishResult`
→ durable `succeeded`

Anything rejected before `running` is a known preflight failure. If uncertainty begins after the upload starts, PR27 records `outcome_unknown` rather than automatically creating a potentially duplicate video.

## Provider identity and immutable PR28 policy

The concrete provider identity is:

- provider ID: `youtube`;
- provider version: `youtube_data_api_v3_pr28_v1:category=22:notify=0`;
- destination identity: exact YouTube channel ID.

PR28 fixes two YouTube-specific values as immutable adapter policy:

- upload category `22` (`People & Blogs`);
- `notifySubscribers=false`.

They are intentionally **not runtime configuration**. Neither value exists in the PR27 human-approved request, so allowing an operator to change them after approval would permit the same approval digest to produce different remote semantics. A later PR can add category or notification behavior only by extending/versioning the approved publish contract.

`health()` loads the local OAuth token, refreshes it when needed, creates the YouTube Data API client, and calls `channels.list(mine=true)` to prove the token resolves to exactly the configured channel. A successful health check pins that service on the current execution thread so later preflight/upload uses the credential context verified immediately before the remote boundary.

## Provider preflight

A real platform has narrower constraints than portable PR27 metadata. PR28 therefore adds an optional `PublishingPreflightProvider` capability and runs it after provider health but before `prepared → running`.

Preflight may perform local work and read-only provider checks, but it must not begin the publication side effect.

YouTube preflight checks:

- exact provider/channel identity;
- stable PR27 idempotency identity;
- exact media byte count against approved artifact evidence;
- YouTube's 256 GB upload ceiling;
- YouTube's 12-hour duration ceiling;
- for videos longer than 15 minutes, a read-only `channels.list(part="status", mine=true)` check requiring `status.longUploadsStatus == "allowed"`;
- fixed category `22` through read-only `videoCategories.list`, requiring it to remain assignable;
- title length and prohibited angle brackets;
- UTF-8 description byte limit and prohibited angle brackets;
- YouTube tag-budget accounting;
- scheduled publication only for approved `public` visibility;
- whole-second schedule precision;
- schedule time strictly in the future.

### Authenticated upload snapshot

The pathname authenticated by the render layer cannot safely be handed to a remote uploader later: another process could replace or rewrite the file between artifact verification and `MediaIoBaseUpload` reading it.

PR28 therefore copies the candidate media into a private temporary file **before** `running` while simultaneously computing SHA-256 and byte count. The snapshot must match both `PublishArtifactRef.output_sha256` and `bytes_written`. Only that already-authenticated, seekable snapshot handle is supplied to the Google upload object.

Consequences:

- same-sized replacement of the source file is detected during snapshot authentication if it happens before/during the copy;
- replacement after preflight cannot change the bytes uploaded, because the uploader reads the snapshot rather than reopening the source pathname;
- snapshot/hash failure remains a retryable preflight `failed` outcome;
- the snapshot handle is closed after the remote upload attempt, including exceptional paths.

After the snapshot exists, preflight locally constructs `MediaIoBaseUpload` and the `videos.insert` request object. The actual publication side effect begins only after the durable transition to `running`, when `next_chunk()` is invoked.

This distinction is intentional: local request-construction failures, invalid platform capability, invalid metadata, and artifact-byte mismatches remain retryable `failed`; only uncertainty after a video upload may have begun becomes `outcome_unknown`.

The generic `PublishMetadata` remains platform-agnostic; YouTube constraints do not leak into other future providers.

## Upload semantics

PR28 uses the Google API Python client's resumable path:

- `MediaIoBaseUpload(<authenticated snapshot handle>, resumable=True)` with 8 MiB chunks;
- `videos.insert(part="snippet,status", ...)` request construction during preflight;
- `snippet.categoryId = "22"` as immutable PR28 policy;
- `notifySubscribers=false` as immutable PR28 policy;
- remote upload through `next_chunk(num_retries=...)` only after `running`.

Using finite resumable chunks preserves meaningful interruption recovery rather than degenerating the resumable API into one large request.

For an unscheduled upload, approved visibility is passed directly as `status.privacyStatus`.

For a scheduled public request, YouTube requires a private video plus an exact future `publishAt`:

```json
{
  "status": {
    "privacyStatus": "private",
    "publishAt": "<approved UTC instant>"
  }
}
```

PR28 therefore accepts scheduling only when the approved semantic visibility is `public`. The remote object must verify as private with the exact approved `publishAt` before Content Forge stores a `scheduled` result.

## Processing and remote verification

A successful `videos.insert` response only proves that YouTube accepted the upload transaction; it is not sufficient durable evidence that YouTube successfully processed the video.

PR28 polls `videos.list(part="snippet,status", id=<video_id>)` in a bounded verification window:

- `status.uploadStatus == "processed"` permits final metadata/privacy verification;
- `failed`, `rejected`, or `deleted` fail closed;
- `uploaded` means processing is still pending and is polled again;
- any undocumented/malformed status fails closed;
- if the bounded window ends while status is still `uploaded`, no success receipt is produced.

Because this verification happens after the remote upload boundary, a terminal failure or still-pending outcome propagates through `PublishingService` as `outcome_unknown`; automatic replacement upload remains blocked until an operator reconciles the remote state.

Once `processed`, PR28 verifies:

- exact video ID;
- exact configured channel ID;
- exact approved title;
- exact approved description;
- exact approved tags;
- fixed category `22`;
- requested privacy for unscheduled uploads;
- `private` plus exact `publishAt` for scheduled uploads.

Only then is `PublishResult` returned. The persisted public URL is a real query-free YouTube short link compatible with PR27's no-query/no-fragment URL invariant:

`https://youtu.be/<video_id>`

## OAuth and credential storage

Install the optional runtime:

```bash
python -m pip install -e ".[youtube]"
```

Create a Google Cloud OAuth client for a desktop/installed application, enable YouTube Data API v3, and download its client-secrets JSON.

Authorize Content Forge locally:

```bash
content-forge-youtube-auth \
  --client-secrets /path/to/client_secret.json \
  --token /private/path/youtube-token.json
```

The installed-app OAuth flow requests only:

- `https://www.googleapis.com/auth/youtube.upload`;
- `https://www.googleapis.com/auth/youtube.readonly`.

The command prints the exact authorized channel ID. The authorized-user token is written atomically to the explicit local path. Final-component token symlinks are rejected; on POSIX, runtime token loading also requires owner-only mode and ownership by the current user. Refresh persistence uses atomic replacement and does not widen permissions.

The OAuth client-secrets file and authorized-user token are never:

- copied into the repository;
- accepted through Content Forge API/PWA publishing JSON;
- stored in `PublishRequest`;
- hashed into publish identity;
- written to the SQLite publishing ledger;
- returned in `PublishResult`.

Start the API explicitly with YouTube publishing enabled:

```bash
content-forge-api \
  --publishing-provider youtube \
  --youtube-token /private/path/youtube-token.json \
  --youtube-channel-id UC...
```

Without `--publishing-provider youtube`, the runtime remains provider-free and cannot perform remote publishing.

## Current YouTube platform constraints

The YouTube Data API / YouTube Help currently document that:

- titles are limited to 100 characters;
- descriptions are limited to 5000 UTF-8 bytes;
- tag accounting is limited to 500 characters;
- uploads are limited to 256 GB or 12 hours, whichever is less;
- videos longer than 15 minutes require channel long-upload eligibility/enablement;
- uploaded videos require valid category metadata;
- `status.publishAt` applies only while a video is private and before it has been published;
- `status.uploadStatus` can be `uploaded`, `processed`, `failed`, `rejected`, or `deleted`;
- YouTube exposes processing progress specifically for polling while an uploaded video is being processed;
- uploads from API projects created after July 28, 2020 that have not passed YouTube's API audit are restricted to private viewing.

That last rule is external platform governance, not something Content Forge can or should bypass. An upload can succeed while public visibility remains unavailable until the Google/YouTube API project satisfies the applicable audit requirements.

YouTube also exposes upload semantics such as `status.selfDeclaredMadeForKids` and `status.containsSyntheticMedia`. PR28 intentionally does **not** inject local defaults for those fields. They can affect the meaning/compliance state of the publication and therefore should become explicit, human-approved publish semantics in a future versioned contract rather than mutable provider-local configuration hidden outside the PR27 request digest.

Until that contract exists, the operator remains responsible for satisfying applicable YouTube audience and altered/synthetic-media disclosure requirements through platform settings/workflow where needed.

## Idempotency and crash safety

YouTube's upload endpoint does not expose a client-supplied idempotency primitive equivalent to PR27's `cfp-<sha256>` identity. Content Forge retains that key as local exact-attempt evidence but never assumes a post-boundary network retry is duplicate-safe.

Therefore:

- failures before `running` may become retryable `failed`;
- uncertainty after `running` becomes `outcome_unknown`;
- replacement upload is blocked until the operator reconciles remote state.

## Non-goals

PR28 does not add analytics ingest, thumbnail upload, playlists, captions, category selection, made-for-kids publish semantics, synthetic-media disclosure publish semantics, monetization settings, subscriber-notification controls, automatic background scheduling, automatic retry of unknown outcomes, another publishing platform, or public-internet exposure.
