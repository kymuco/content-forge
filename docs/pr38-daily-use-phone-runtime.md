# PR38 — Daily-use phone runtime and onboarding

## Goal

PR38 changes the immediate product priority from adding more analytics semantics to making the already-complete PR31–PR35 phone production loop practical to use every day.

The production authority does not change. The phone remains a control surface over the existing local Project / Review / Render / Publishing contracts, while the desktop remains the source of truth and compute worker.

PR38 adds a **machine-local daily-use runtime profile** so transport and optional-provider configuration is supplied once and reused on later launches instead of being reconstructed from a long CLI command every time.

## Daily-use profile

The profile is stored as `daily-use.json` under the existing Content Forge runtime root. It is operational configuration, not Project or reproducibility evidence.

It contains only the configuration needed to start the existing runtime consistently:

- stable HTTPS phone-visible origin;
- desktop bind host and port;
- TLS certificate and private-key paths;
- FFmpeg / ffprobe command paths;
- optional TTS provider selection;
- optional publishing provider selection;
- provider-local YouTube token path and exact configured channel ID when YouTube publishing is enabled.

The profile does **not** contain OAuth token contents, bearer sessions, pairing codes, Project IDs, render IDs, publish attempt IDs, or analytics observations.

## Commands

PR38 adds the `content-forge-daily` entry point.

One-time setup:

```text
content-forge-daily setup \
  --phone-url https://content-forge.example.test:8765 \
  --ssl-certfile /path/to/content-forge.crt \
  --ssl-keyfile /path/to/content-forge.key
```

Optional YouTube publishing configuration remains explicit:

```text
content-forge-daily setup \
  --phone-url https://content-forge.example.test:8765 \
  --ssl-certfile /path/to/content-forge.crt \
  --ssl-keyfile /path/to/content-forge.key \
  --publishing-provider youtube \
  --youtube-token /private/path/youtube-token.json \
  --youtube-channel-id UC...
```

Repeatable readiness check:

```text
content-forge-daily doctor
```

Normal launch:

```text
content-forge-daily run
```

`CONTENT_FORGE_HOME` and `--root` retain the existing runtime-root authority. PR38 does not introduce a second data directory.

## Preflight and fail-closed behavior

The daily launcher refuses to start when its persisted operational authority is incomplete. `setup` performs the same readiness checks before replacing the persisted profile, so a bad new configuration does not overwrite the last saved profile merely because its JSON shape was valid.

The profile contract requires:

- an HTTPS phone-visible origin with no URL path;
- a host that is meaningful from the phone, rejecting `localhost`, loopback IPs, and unspecified IPs such as `0.0.0.0`;
- a valid non-loopback transport configuration with both TLS certificate and key paths;
- regular readable TLS paths;
- no final-component symlink for daily-use files;
- current-user ownership and owner-only permissions for private files on POSIX;
- an available runtime directory;
- executable/discoverable FFmpeg and ffprobe commands;
- both token path and exact channel ID when YouTube publishing is selected.

Private path values are canonicalized only **after** the supplied path itself is checked, so a final-component symlink cannot be hidden by resolving it first.

The profile file itself is written through a randomized exclusive temporary file, fsynced, atomically replaced in the runtime root, and owner-only on POSIX. Exact provider secret contents remain in their existing provider-local files.

## PWA onboarding projection

`create_app()` gains one optional `public_base_url` configuration value. When supplied by the daily profile, the packaged PWA config exposes the normalized public base URL as non-secret onboarding configuration. The served PWA controller then prefills the existing desktop onboarding field from that value, so generating a fresh pairing QR no longer requires retyping the phone-visible address on each launch.

This does not create pairing authority. Pairing challenge creation remains restricted to the existing PR8/PR9 loopback client + loopback Host + loopback Origin boundary, and the generated challenge/code remains short-lived.

The phone still receives the challenge only through the existing local QR onboarding flow and stores only the resulting bearer session in its mount-scoped IndexedDB state.

### Installed-PWA upgrade path

PR35's installed shell used cache revision `v21` and cached `app.js`. Merely changing the server-side controller would therefore be insufficient for an already-installed phone: the old shell could continue serving the pre-PR38 controller from its existing cache.

PR38 serves a deterministic Service Worker composition that advances the active shell cache to `v22`, explicitly names PR35's `v21` cache as the immediate predecessor, and removes the predecessor during activation under the existing scoped cache-cleanup contract. The new shell then caches the served PR38 `app.js`, so an existing installation receives the onboarding-prefill behavior rather than requiring a reinstall.

The cache revision changes UI delivery only. IndexedDB pairing/session state and queued shares remain governed by their existing storage contracts; PR38 does not clear them as part of the shell upgrade.

## What PR38 deliberately does not do

PR38 does not:

- weaken LAN HTTPS requirements;
- generate or install a local certificate authority silently;
- trust a self-signed certificate on the phone automatically;
- expose Content Forge to the public Internet;
- add cloud state or a cloud relay;
- auto-approve review or publishing decisions;
- auto-execute publication;
- change Project / Review / Render / Publishing semantics;
- continue PR38's former analytics-history scope.

OS auto-start and friction discovered during real dogfooding should be driven by actual daily use rather than being guessed into this first deployment PR.

## Roadmap consequence

The former planned `PR38 — Durable performance history and observation windows` is deferred, not abandoned. PR36 and PR37 already provide trustworthy generic and YouTube observation evidence.

The immediate sequence becomes:

```text
PR38  daily-use phone runtime/onboarding
-> real phone dogfooding and friction hardening
-> resume durable performance history
-> experiments / comparison dashboard / recommendations
```

This lets later analytics work consume real Content Forge-produced publications instead of extending an evidence system before the product is comfortably usable.
