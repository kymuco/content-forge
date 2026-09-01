# PR21 — Persistent Voice Cast registry

PR21 adds reusable voice identity on top of the accepted PR19 dialogue model and the PR20 per-line TTS boundary.

The central rule is deliberate:

> a PR19 character is narrative identity; a PR21 cast entry is reusable voice identity.

A project character may be bound to a persistent cast voice, but the two identifiers never become interchangeable. PR21 therefore does not retrofit `character_id` into the TTS provider contract and does not turn provider-specific `voice_id` values into narrative character identity.

## Runtime-wide registry

`VoiceCastRegistry` stores immutable revisions in the local runtime SQLite catalog. A `VoiceCastDefinition` contains:

- a readable `cast_id` such as `protagonist`, `secondary-a`, or `narrator`;
- a display name;
- the semantic `LineTTSSettings` recipe already defined by PR20.

Writing an identical definition is idempotent and returns the current revision. Any semantic change creates the next integer revision. Existing revisions remain addressable by exact `(cast_id, revision)`.

The registry has its own `application_schema` component (`voice_cast`) so it can coexist with the Inbox/auth application tables without changing the core storage schema version.

## Immutable reference-audio identity

A cast recipe may retain a PR20 reference-audio asset for cloning. Asset ID alone is not sufficient immutable evidence: catalog corruption or an invalid metadata substitution must not let one historical cast revision begin to mean different bytes.

For that reason every cast revision with a reference asset also retains the exact verified reference-audio SHA-256. The SHA participates in `definition_sha256` and is rechecked on every registry read.

This closes two distinct failure modes:

1. missing/corrupt bytes are rejected by the content-addressed asset verifier;
2. different but otherwise valid bytes appearing behind the same retained asset ID are rejected against the pinned revision SHA.

Project-local TTS overrides use the same rule. If an override references audio, its binding stores `settings_override_reference_sha256` and revalidates that exact digest before resolution or synthesis.

## Project bindings

A project's `pr21_voice_cast` metadata contains `CharacterCastBinding` records. Each binding stores:

- PR19 `character_id`;
- `cast_id`;
- exact `cast_revision`;
- exact cast `definition_sha256`;
- optional full project-local `LineTTSSettings` override;
- exact override reference-audio SHA when an override uses reference audio.

Bindings pin a revision. Creating a newer global revision never silently changes an existing project.

A project may explicitly rebind a character to the latest or another chosen revision. A project-local override changes only that binding; it never mutates the runtime-wide cast definition.

A real bind/rebind/unbind also removes existing PR20 synthesized-line receipts for every accepted dialogue line spoken by that character. The immutable generated audio blobs are not deleted, but they stop being represented as current Project synthesis authority immediately. This prevents old voice audio from looking current during the interval before the next PR21/PR20 synthesis. Re-applying the exact same binding is idempotent and does not invalidate matching materialized audio.

PR21 mutations are allowed only while the project remains in the same editable states used by PR20 (`draft`, `prepared`, or `ready`). If the retained PR20 TTS manifest is malformed, cast mutation fails closed rather than rewriting around corrupted synthesis state.

## Resolution into PR20

`VoiceCastWorkflow.resolve_line()` performs the following checks before returning an effective voice:

1. load one exact Project snapshot;
2. revalidate the accepted PR19 dialogue authority from that snapshot;
3. validate every retained character binding against the registered PR19 characters;
4. load the exact pinned cast revision and verify its definition digest;
5. verify reference bytes and their pinned SHA values;
6. select either the project override or the immutable cast recipe.

The result is a `ResolvedLineVoice` containing both cast evidence and the effective PR20 `LineTTSSettings`.

PR21 deliberately does not add provider/model checkpoint identity to the cast definition. Provider ID, provider version, model ID, immutable model revision, provider configuration, and semantic request digest remain PR20 invocation evidence because they describe the actual synthesis runtime rather than the reusable human-facing cast recipe.

## TOCTOU boundary

Resolving a cast and then starting an expensive TTS call creates a possible time-of-check/time-of-use race if the Project changes between those two operations.

PR21 therefore feeds synthesis through a guarded PR20 workflow. The Project JSON observed when cast resolution begins must be byte-for-byte identical to the Project snapshot PR20 reads before synthesis. A concurrent character/cast/dialogue change fails closed before the provider is called.

PR20 then retains its existing compare-and-swap commit boundary for the generated line record.

## Cache behavior

PR21 does not introduce a second audio cache.

Once a cast binding resolves to `LineTTSSettings`, synthesis uses the normal PR20 semantic request digest and provider/model/config cache identity. Therefore:

- repeated preview/synthesis of the same line and effective voice can reuse the existing verified PR20 line asset;
- changing voice/style/reference/generation settings invalidates that line's cache naturally;
- merely creating a newer global cast revision does not invalidate projects still pinned to an older unchanged recipe;
- a real bind/rebind/unbind removes affected current Project receipts immediately;
- rebinding to a semantically different revision produces the corresponding new PR20 request/cache identity when synthesized again.

## HTTP surface

PR21 installs an authenticated local API under `/api/v1/voice-cast`:

- `GET /api/v1/voice-cast` — list latest reusable cast revisions;
- `POST /api/v1/voice-cast` — create the first revision or append a semantic revision;
- `GET /api/v1/voice-cast/projects/{project_id}` — inspect PR19 characters and their cast bindings;
- `PUT /api/v1/voice-cast/projects/{project_id}/characters/{character_id}` — pin/rebind a character and optional project override;
- `DELETE /api/v1/voice-cast/projects/{project_id}/characters/{character_id}` — remove the project binding;
- `POST /api/v1/voice-cast/projects/{project_id}/characters/{character_id}/preview` — synthesize/play a representative accepted line through PR20.

The route family preserves the existing local API transport boundary: non-loopback use requires HTTPS, authentication occurs before body parsing, JSON writes are content-type checked and size bounded, and no runtime filesystem path is exposed.

The TTS provider is optional at API construction time. Registry and binding operations work without Qwen/Torch or another TTS runtime. Preview returns a controlled unavailable response if no provider is configured.

For the normal CLI server, preview synthesis is explicitly opt-in:

```text
content-forge-api --tts-provider qwen
```

Install the PR20 `tts` optional extra first. Without `--tts-provider qwen`, `content-forge-api` remains provider-free. Selecting Qwen constructs the existing pinned PR20 adapter but still does not load/download model weights at server startup; the heavy runtime remains lazy until synthesis actually needs it.

## Preview semantics

Voice preview intentionally reuses real accepted project dialogue rather than inventing a second free-text preview artifact. PR21 selects the first accepted PR19 line spoken by the chosen character and sends it through the normal guarded PR20 synthesis path.

The HTTP response is the verified content-addressed WAV asset. The response exposes cast revision and audio SHA headers for debugging/evidence, while the underlying generated asset remains ordinary PR20 project state and cache authority.

This means a preview can warm the real line cache instead of creating disposable duplicate audio.

## PWA surface

The phone/desktop PWA has a dedicated Voice Cast panel because PR19 review cards disappear after dialogue acceptance while persistent cast management must remain accessible afterward.

The panel supports:

- creating a reusable cast recipe;
- loading a recent or pasted project ID;
- assigning the latest cast revision to each accepted PR19 character;
- applying an optional project-only voice-ID override;
- unassigning a character;
- generating and playing a voice preview through the authenticated API.

Voice Cast actions surface request/validation failures in the panel instead of leaving unhandled browser promise rejections, and starting a new preview stops/revokes the previous preview object URL so repeated checks do not overlap indefinitely.

The service worker advances the shell cache to `v10`, precaches `voice-cast.js`, and explicitly cleans both retained predecessor namespaces (`v8` and `v9`) so already-installed older shells can upgrade without stale UI authority.

This is intentionally not the PR22 panel-centric voiced-story editor. Fine-grained listen/regenerate controls, timed text, scene duration editing, and full voiced-story review remain PR22.

## Deferred boundaries

PR21 does not own:

- automatic speaker inference — PR19 remains explicit human-approved dialogue authority;
- phrase/word timing or forced alignment — PR22;
- scene duration derived from synthesized dialogue — PR22;
- dialogue/ambience/music sequencing and ducking — PR23;
- camera choreography around speakers — PR23;
- channel/profile-specific cast selection — retained as a later extension of the cast registry;
- publishing or remote voice services.

## Tests

PR21 regression coverage includes:

- immutable/idempotent cast revision creation;
- coexistence with the existing application schema component;
- exact reference-audio SHA pinning and byte verification;
- rejection of a valid alternate blob substituted behind a historical asset ID;
- project override reference SHA requirements;
- revision pinning when newer global revisions appear;
- project-only overrides without global mutation;
- reuse of one cast revision across projects;
- PR20 cache reuse and invalidation after rebinding;
- affected PR20 Project receipts disappearing when cast authority changes;
- rejection of a Project change between cast resolution and PR20 snapshot;
- authenticated registry/binding/preview HTTP behavior;
- bounded pre-parser transport behavior;
- optional-provider preview failure;
- explicit lazy CLI Qwen provider selection;
- versioned PWA shell precaching for Voice Cast;
- real WAV preview publication through the existing PR20 asset/cache path.
