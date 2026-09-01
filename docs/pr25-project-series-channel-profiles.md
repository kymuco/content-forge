# PR25 — Project / series / channel production profiles

PR25 introduces reusable production defaults without turning a channel or series profile into a second live authority over an existing `Project`.

## Core rule

A production profile is a **revisioned source of defaults**. A Project only changes after an explicit bind operation, and that operation stores the exact immutable profile revision inside Project metadata.

Changing the runtime-wide profile later does not silently change Projects that were already bound to an earlier revision.

## Profile scopes

The first contract supports three semantic scopes:

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

This prevents a reusable profile from becoming a bag of unverified string identifiers.

## Project binding

`ProductionProfileWorkflow.bind()` performs one Project CAS.

The Project stores `pr25_production_profile`, containing the complete exact `ProductionProfileRevision` snapshot plus two ownership flags describing whether PR25 actually filled:

- the Project template;
- the Project output-profile tuple.

PR25 only fills these core fields when they were previously absent. Existing explicit Project template/output choices win and are not overwritten.

Branding, skin, cast-role defaults, languages, credit policy and asset libraries remain retained defaults in the immutable profile snapshot in this first slice. Their later consumers must apply them explicitly rather than treating the registry as hidden live configuration.

## Fail-closed ownership

When PR25 filled a Project template or output profiles, subsequent PR25 manifest validation requires those exact values to remain present. Drift is a conflict rather than silently accepting a Project whose recorded ownership no longer matches its core state.

Binding the same exact revision is idempotent. Rebinding to another revision is intentionally deferred until reversible replacement semantics are specified and tested; the first slice rejects it explicitly instead of guessing which later Project edits should be preserved.

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

It only provides reusable, exact, revisioned defaults and a bounded explicit Project snapshot operation.

## First-slice non-goals

Deferred within PR25:

- profile create/edit/list API;
- PWA profile management;
- reversible rebind/unbind;
- automatic narrative-character to cast-role assignment;
- automatic variant creation from language defaults;
- automatic watermark/music/reaction materialization;
- implicit mutation of already-reviewed or rendered Projects.

Those layers should build on the snapshot contract rather than weakening it.
