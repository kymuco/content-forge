# PR28 — YouTube Data API publishing adapter

## Goal

Turn the PR27 platform-agnostic publish boundary into one real remote publishing path without making YouTube, Google OAuth, or network availability part of rendering/export correctness.

PR28 adds a YouTube Data API v3 adapter only. It does not add analytics, a second platform adapter, or an automatic scheduler daemon.

## Runtime boundary

The authority chain remains:

`final RenderArtifactManifest`
→ authenticated artifact loader
→ exact credential-free `PublishRequest`
→ explicit human approval
→ durable `prepared` attempt
→ provider health/channel verification
→ YouTube-specific preflight
→ durable `running` remote boundary
→ resumable `videos.insert`
→ remote `videos.list` verification
→ authenticated `PublishResult`
→ durable `succeeded`

If anything fails after the resumable upload call begins, PR27 records `outcome_unknown` rather than automatically retrying a potentially duplicated publication.

## Provider identity

The concrete provider identity is:

- provider ID: `youtube`
- provider version: `youtube_data_api_v3_pr28_v1`
- destination identity: exact YouTube channel ID

The configured channel ID is local runtime configuration and the same credential-free ID must be present in the human-approved `PublishTarget.destination_id`.

`health()` loads the local OAuth token, refreshes it when needed, creates the YouTube Data API client, and calls `channels.list(mine=true)` to prove the token currently resolves to exactly the configured channel.

A successful health check pins that authenticated service on the current execution thread so the later upload uses the same credential context that was verified immediately before the remote boundary.

## Provider preflight

PR27's original provider contract intentionally had only `health()` and `publish()`. A real platform exposes narrower constraints than the portable publish model, so PR28 adds an optional duck-typed provider `preflight(...)` capability in `PublishingService`.

The order is deliberate:

1. authenticate the final artifact;
2. verify provider health/identity;
3. run provider preflight;
4. only then transition `prepared → running`;
5. call `publish()`.

If preflight rejects the request, the durable attempt becomes retryable `failed`; it never crosses the remote-side-effect boundary.

The YouTube preflight checks:

- exact provider/channel identity;
- stable PR27 idempotency identity;
- exact media byte count against the approved artifact;
- YouTube's upload-size ceiling;
- title length and character restrictions;
- UTF-8 description byte limit and character restrictions;
- YouTube tag-budget accounting;
- scheduled publication only for approved `public` visibility;
- whole-second schedule precision;
- schedule time strictly in the future.

The generic PR27 `PublishMetadata` remains platform-agnostic and is not narrowed globally to YouTube limits.

## Upload semantics

PR28 uses the Google API Python client's resumable media upload path:

- `videos.insert(part="snippet,status", ...)`;
- `MediaFileUpload(..., resumable=True)`;
- retry support delegated to the Google client `next_chunk(num_retries=...)`.

For unscheduled uploads, the approved visibility is passed directly as `status.privacyStatus`.

For a scheduled public request, YouTube requires creation as:

```json
{
  "status": {
    "privacyStatus": "private",
    "publishAt": "<approved UTC instant>"
  }
}
```

PR28 therefore accepts scheduling only when the approved semantic visibility is `public`. The remote object is verified to remain private with the exact approved `publishAt` before Content Forge stores a `scheduled` result.

## Remote verification

A successful `videos.insert` response is not sufficient durable evidence by itself.

After upload, PR28 calls `videos.list(part="snippet,status", id=<video_id>)` and verifies:

- exact video ID;
- exact configured channel ID;
- requested privacy for unscheduled uploads;
- `private` plus exact `publishAt` for scheduled uploads.

Only then is a `PublishResult` returned.

The stored public remote URL is canonical and contains no query or fragment:

`https://www.youtube.com/watch/<video_id>`

## OAuth and credential storage

Install the optional runtime:

```bash
python -m pip install -e ".[youtube]"
```

Create a Google Cloud OAuth client for a desktop/installed application, enable YouTube Data API v3, and download the client-secrets JSON.

Authorize Content Forge locally:

```bash
content-forge-youtube-auth \
  --client-secrets /path/to/client_secret.json \
  --token /private/path/youtube-token.json
```

The command uses Google's installed-application OAuth flow with the minimal scopes needed by PR28:

- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/youtube.readonly`

It prints the exact authorized YouTube channel ID. The token is written atomically to the explicit local path and is owner-only on POSIX systems.

The OAuth client-secrets file and authorized-user token are never:

- copied into the repository;
- accepted through Content Forge API/PWA publishing JSON;
- stored in `PublishRequest`;
- hashed into publish identity;
- written to the SQLite publishing ledger;
- returned in `PublishResult`.

Start the API with the explicit provider:

```bash
content-forge-api \
  --publishing-provider youtube \
  --youtube-token /private/path/youtube-token.json \
  --youtube-channel-id UC...
```

Without `--publishing-provider youtube`, Content Forge remains provider-free and cannot perform remote publishing.

## Important YouTube platform constraints

YouTube's current API documentation states that:

- video titles are limited to 100 characters;
- descriptions are limited to 5000 bytes;
- tag accounting is limited to 500 characters;
- `status.publishAt` can only be used while the video is private and before it has ever been published;
- resumable uploads are the recommended reliability path for large/interruption-prone uploads;
- uploads made through API projects created after July 28, 2020 that have not passed YouTube's API audit are restricted to private viewing.

That last rule is external platform governance, not something Content Forge can bypass. A developer project may successfully upload while still being unable to make the video public until the project satisfies YouTube's audit requirements.

## Idempotency and crash safety

YouTube's upload endpoint does not provide the same client-supplied idempotency primitive represented by PR27's `cfp-<sha256>` identity.

Content Forge therefore keeps that key as local exact-attempt evidence, but never assumes a network retry is duplicate-safe after the remote upload boundary.

This is why the PR27 rule remains essential:

- failures before `running` can become retryable `failed`;
- uncertainty after `running` becomes `outcome_unknown`;
- automatic replacement uploads are blocked until the operator reconciles the remote state.

## Non-goals

PR28 does not add:

- YouTube analytics ingest;
- thumbnail upload;
- playlists;
- captions;
- made-for-kids UI;
- synthetic-media disclosure UI;
- monetization settings;
- automatic background scheduling;
- automatic retry of unknown upload outcomes;
- TikTok/Instagram/other publishing adapters;
- public-internet exposure.

Those should be added only as separately reviewable capabilities after this first real publishing path is proven.
