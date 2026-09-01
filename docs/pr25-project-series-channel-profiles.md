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

Before a profile revision is accepted or read as current, PR25 validates its external references:

- template ID/version must exist in the supplied template registry;
- an optional skin must exist and be declared by the selected template;
- cast defaults must resolve to the exact PR21 revision and definition digest;
- pinned local assets must exist, retain the exact SHA-256, have a compatible media type, and pass content-store verification.

The exact revision stored by a Project is revalidated against the registry before PR25-owned state is trusted, replaced, or removed. This prevents a reusable profile from becoming a bag of unverified string identifiers or a stale retained snapshot whose external references are no longer valid.

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
2. revalidates the exact retained registry revision and all external references;
3. verifies any PR25-owned template/output values still exactly equal the retained definition;
4. removes only the values whose ownership flags are true;
5. preserves explicit Project values that PR25 never owned.

A rebind then applies the new revision to that reconstructed base and commits the complete change with one CAS against the original persisted Project snapshot. There is no persisted intermediate Project with old profile metadata and partially replaced defaults.

Binding the same exact revision is idempotent. `unbind()` removes the PR25 metadata and only PR25-owned defaults. Drift of owned state is a conflict rather than a best-effort restore.

Mutations are limited to `INBOX`, `DRAFT`, and `PREPARED`. PR25 does not silently alter already-reviewed/ready/rendered Projects, so it does not need to invent a parallel preview-approval invalidation path.

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

## Remaining PR25 layers

Still to add on top of this authority contract:

- authenticated profile create/list/get and Project bind/rebind/unbind API;
- PWA profile management/selection;
- bounded consumers for language, credit, cast-role, branding, music and reaction defaults where they can be applied without stealing authority from PR16/PR21/provenance/PR14;
- adversarial corruption/TOCTOU regressions for the final surface.

Automatic narrative-character to cast-role guessing and implicit mutation of already-reviewed/rendered Projects remain non-goals.
