# PR11 — Template registry, skins, slots, and component contracts

## Purpose

PR11 turns the existing one-template proof into a versioned extension boundary without
adding template-specific branches to the timeline compiler or FFmpeg backend.

The runtime boundary is:

```text
Project.template = (template_id, version)
             |
             v
      TemplateRegistry
             |
   exact version lookup
             |
             v
 declarative TemplateDefinition
 + trusted built-in resolver
             |
             v
      ResolvedTemplate
 Scene + Overlay + AudioTrack
 + registry evidence
             |
             v
      compile_timeline
             |
             v
         RenderPlan
```

A template remains presentation policy. `ContentKind`, `Workflow`, and `OutputProfile`
remain separate identities.

## Declarative contracts

`content_forge.templates.contracts` defines immutable Pydantic contracts for:

- versioned `ComponentDefinition` and `ComponentRef`;
- versioned `TemplateDefinition`;
- slots with component identity, kind, geometry, anchor, and requiredness;
- normalized anchors;
- normalized template safe zones with explicit policy;
- per-template defaults;
- versioned reusable `SkinDefinition` / `SkinRef`;
- digest-bound packaged `TemplateAssetDefinition` records.

A `TemplateDefinition` rejects duplicate IDs, dangling slot-component references, and
dangling anchor references before registration. Registration additionally rejects text or
asset/media slots whose referenced component does not advertise the required capability.

`validate_slot_bindings()` is deliberately bounded: it rejects unknown slots, missing
required slots, non-JSON binding structures, incompatible primitive binding types, and
raw paths/arbitrary strings where a Content Forge asset identity is required. It does so
without executing template or plugin code. Template-specific semantic validation remains
owned by the registered resolver.

## Registries

PR11 adds four registries:

```text
TemplateAssetRegistry
ComponentRegistry
SkinRegistry
TemplateRegistry
```

All identities are exact and versioned where applicable. Duplicate exact identities fail
closed. Templates may register only after every referenced component and skin is already
known. Skins may include only known packaged assets marked redistributable.

`create_builtin_registries()` returns a fresh bundle rather than a mutable process-global
singleton, preventing one caller's registration from silently changing another caller's
built-in view.

## Generic compile path

`TemplateRegistry.resolve()` selects the exact `Project.template` identity and exact
profile/variant selection before invoking only that registration's trusted resolver. It
independently checks that the resolver returns the same template ID/version and the same
profile/variant bindings it was called with.

`TemplateRegistry.compile()` then sends the resulting `ResolvedTemplate` through the
existing generic `compile_timeline()` path. No template-specific renderer dispatch is
introduced.

The existing `hook_overlay` resolver is registered as the first built-in template. The
public `content_forge.templates.compile_hook_overlay()` compatibility entry point is
registry-backed from PR11 onward, so the existing PR10 preview/final production workflow
automatically receives the same exact-version and provenance guarantees without changing
its review authority or state machine. The low-level direct compiler remains internal to
`content_forge.templates.hook_overlay` as a semantic regression baseline.

The registered plan is required to be semantically equal to that direct baseline after
removing PR11's reserved registry-evidence property. The complete plans intentionally have
different render-plan digests because registry provenance is now part of render evidence.

## Registry evidence

A resolver cannot write the reserved `content_forge_registry_evidence_v1` property.
`TemplateRegistry` adds it only after identity/binding validation as canonical JSON so it
survives the existing immutable Pydantic boundaries without requiring changes to
`ResolvedTemplate`, `RenderPlan`, or `compile_timeline`.

The evidence records:

- exact template ID/version plus SHA-256 of its declarative definition;
- every declared component ID/version plus SHA-256 of its definition;
- every declared skin ID/version plus SHA-256 of its definition;
- digest and SPDX identity of packaged assets referenced by those skins.

Because `ResolvedTemplate.properties` already flows into `RenderPlan.template_properties`,
this evidence is automatically included in the existing `render_plan_digest()`. Reusing a
version string with changed declarative contract bytes therefore changes the resulting
semantic plan identity instead of becoming invisible provenance drift.

## Built-in components and skin fixture

PR11 registers the first component contracts:

- `media@1.0` -> scene output;
- `text@1.0` -> overlay output;
- `original_audio@1.0` -> audio output.

The contract also reserves the `transition` output kind for the already-planned reusable
transition components without implementing PR13 rendering behavior early.

PR11 adds a small project-owned neutral skin fixture and packaged SVG asset. The asset is
Apache-2.0, explicitly redistributable, and SHA-256 bound in the registry. CI verifies the
packaged bytes against the declared digest.

The neutral skin is an extension fixture, not a change to `hook_overlay` rendering. PR12
can consume skins/assets while keeping PR11's registry semantics unchanged.

## Plugin discovery boundary

PR11 reserves metadata entry-point groups:

```text
content_forge.templates
content_forge.components
content_forge.skins
```

`discover_plugin_candidates()` enumerates and sorts only installed entry-point metadata.
It never calls `EntryPoint.load()`, so discovery does not import plugin modules or invoke
their registration/runtime code.

PR11 intentionally does **not** define plugin trust, installation, loading, sandboxing,
compatibility migration, or runtime execution. Those require a separate authority and
security design.

## Versioning rules

- template identity is `(template_id, version)`;
- component identity is `(component_id, version)`;
- skin identity is `(skin_id, version)`;
- packaged template assets are digest-bound by stable `asset_id` + SHA-256;
- declarative definition hashes are carried into render-plan evidence;
- a resolver may not return an identity or profile/variant binding different from its registration call;
- a resolver may not spoof the reserved registry-evidence property;
- unregistered exact versions fail closed rather than falling back to another version.

Version fallback or migration is deliberately absent. A canonical `Project.template`
reference must resolve exactly.

## Scope boundary

PR11 includes:

- declarative schema;
- component/template/skin/asset registries;
- slots, anchors, safe zones, defaults, and validation;
- exact versioning and render-plan registry provenance;
- redistribution-safe built-in fixture;
- metadata-only plugin discovery;
- generic registered template resolution/compilation;
- registry-backed compatibility compilation for the existing `hook_overlay` production path.

PR11 does not include:

- PR12's initial template pack;
- third-party plugin loading or execution;
- plugin installation;
- remote registries/marketplaces;
- new renderer primitives;
- a second timeline representation;
- publishing or worker changes.

The exit condition is satisfied when a future simple template can be added by registering
a declarative definition and resolver that emits existing timeline primitives, without
changing `Project`, `compile_timeline`, or the FFmpeg backend.
