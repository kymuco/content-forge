# PR25 — Project / series / channel production profiles

PR25 introduces reusable production defaults without turning a channel or series profile into a second live authority over an existing `Project`.

## Core rule

A production profile is a **revisioned source of defaults**. A Project only changes after an explicit bind or rebind operation, and that operation stores the exact immutable profile revision inside Project metadata.

Changing the runtime-wide profile later does not silently change Projects that were already bound to an earlier revision.

## Profile scopes

The contract supports three semantic scopes:

- `project` — a reusable preset intended for one project family;
- `series` — defaults shared by a recurring series;
- `channel` — defaults shared by a channel/brand.

Scope is descriptive policy metadata. It does not change Project authority or renderer behavior by itself.

## Revisioned defaults

`ProductionProfileDefinition` can retain:

- default registered `TemplateRef`;
- optional compatible registered `SkinRef`;
- reusable cast-role defaults pinned to exact PR21 cast revision + definition digest;
- default language tags;
- credit policy;
- exact `OutputProfile` values;
- branding metadata and optional exact watermark asset pin;
- exact music-library asset pins;
- exact reaction-library asset pins.

Registry revisions are immutable. Re-putting an identical definition is idempotent; changing the definition creates the next revision.

## External evidence validation

Before a profile revision is accepted or used as current authority, PR25 validates its external references:

- template ID/version must exist in the supplied template registry;
- an optional skin must exist and be declared by the selected template;
- cast defaults must resolve to the exact PR21 revision and definition digest;
- pinned local assets must exist, retain the exact SHA-256, have a compatible media type, and pass content-store verification.

`put`, `get`, latest-revision listing, ordinary Project manifest reads, same-revision binds and target revisions for rebind all preserve that fail-closed rule.

Safe removal has a narrower requirement by design. `unbind()` and removal of an old revision during rebind verify that the immutable registry record still exactly matches the Project snapshot and that PR25-owned Project fields have not drifted, but they do **not** require an old optional cast/music/watermark dependency to remain available. A missing external dependency may make the old profile unusable, but it cannot permanently trap the Project under authority that the user is trying to remove.

## Project binding

`ProductionProfileWorkflow.bind()` performs one Project CAS.

The Project stores `pr25_production_profile`, containing the complete exact `ProductionProfileRevision` snapshot plus two ownership flags describing whether PR25 actually filled:

- the Project template;
- the Project output-profile tuple.

PR25 only fills these core fields when they were previously absent. Existing explicit Project template/output choices win and are not overwritten.

Branding, skin, cast-role defaults, languages, credit policy and asset libraries remain retained defaults in the immutable profile snapshot until a bounded consumer explicitly applies them. They are never read as hidden live configuration from the current registry revision.

## Reversible ownership

PR25 can safely rebind or unbind because it only claims ownership of fields that were empty before its own materialization.

Before replacement/removal it:

1. parses the retained profile manifest;
2. verifies the exact retained immutable registry record still equals the Project snapshot;
3. verifies any PR25-owned template/output values still exactly equal the retained definition;
4. removes only the values whose ownership flags are true;
5. preserves explicit Project values that PR25 never owned.

For a rebind, the **new** target revision is fully externally validated before it is applied. The workflow then applies the target revision to the reconstructed base and commits the complete change with one CAS against the original persisted Project snapshot. There is no persisted intermediate Project with old profile metadata and partially replaced defaults.

Binding the same exact revision is idempotent. `unbind()` removes the PR25 metadata and only PR25-owned defaults. Drift of owned state or immutable revision identity is a conflict rather than a best-effort restore.

Mutations are limited to `INBOX`, `DRAFT`, and `PREPARED`. PR25 does not silently alter already-reviewed/ready/rendered Projects, so it does not invent a parallel preview-approval invalidation path.

## HTTP surface

PR25 installs an authenticated production-profile API through the normal `create_app()` path:

- list latest profile revisions;
- create an immutable revision (idempotent identical definitions return the existing revision);
- get an exact/latest profile revision;
- inspect one Project's exact profile snapshot;
- bind or rebind a Project explicitly;
- unbind a Project explicitly.

The transport boundary requires secure transport where applicable, authenticates before JSON parsing, requires JSON for write bodies, requires `Content-Length`, and caps profile request bodies at 256 KiB.

Project responses are built from one exact Project snapshot: PR25 validates the retained manifest against that same in-memory Project object instead of performing a second Project read, preventing a read-side bind/rebind TOCTOU mix of core fields and profile metadata.

## PWA surface

The packaged PWA exposes a production-profile panel for:

- browsing immutable latest revisions;
- creating/revising the basic reusable profile fields exposed by the browser UI;
- selecting a recent Project;
- explicit bind/rebind to a selected revision;
- explicit unbind;
- showing the exact retained Project revision and whether PR25 owns template/output defaults.

The browser remains an API client rather than a second authority. It does not independently materialize cast, language, credit, branding, music or reaction defaults. Installed shells advance the service-worker cache namespace from v12 to v13 so the new panel cannot be shadowed by the previous cached UI.

## Authority boundaries

PR25 does **not** replace:

- Project as canonical production state;
- PR11 template/skin registries;
- PR21 Voice Cast identity;
- PR16 variant/language authority;
- source provenance and credit/permission evidence;
- PR6/PR24 output-profile values;
- PR14 audio composition;
- PR7/PR17 render identity and artifact evidence.

It only provides reusable, exact, revisioned defaults and bounded explicit Project snapshot operations.

## Deliberate follow-ons / non-goals

PR25 retains several defaults without automatically consuming them. Later bounded integrations may apply language, credit, cast-role, branding, music or reaction defaults only where they can do so without stealing authority from PR16, PR21, provenance or PR14.

Automatic narrative-character to cast-role guessing, hidden live inheritance from a changing profile revision, and implicit mutation of already-reviewed/rendered Projects remain non-goals.
