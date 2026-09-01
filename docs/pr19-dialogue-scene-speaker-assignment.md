# PR19 — Dialogue scene and speaker-assignment contract

PR19 adds the first semantic dialogue layer on top of the retained, human-corrected PR18 panel OCR boundary. It deliberately stops before speech synthesis, persistent voice casting, timed text, and camera choreography.

The authority chain is:

```text
verified PR18 OCR
-> project-local character registry
-> blocking dialogue assignment ReviewTask
-> explicit reading order + speaker assignment
-> retained SceneDialogue
```

Assisted proposals may prefill the review surface, but they never become accepted dialogue without the dedicated PR19 acceptance path.

## Scope

PR19 owns:

- a project-local character registry;
- explicit reading order for every retained OCR region in a scene;
- one registered speaker ID for every dialogue region;
- optional semantic scene focus hints (`speaker`, `face`, or `explicit_crop`);
- a versioned dialogue manifest retained in Project metadata;
- a dedicated blocking review workflow and PWA assignment surface;
- provenance checks that bind accepted dialogue to the exact retained PR18 extraction and current scene media asset.

PR19 does not own:

- TTS generation or speech caches — PR20;
- persistent/global voice identities or character-to-voice casting — PR21;
- the full panel-centric voice/timed-text editor — PR22;
- concrete pan/zoom/focus choreography or voiced-scene audio sequencing — PR23.

The character registry therefore represents narrative identity only. A `character_id` is not a voice ID.

## Retained models

`ProjectDialogueManifest` is stored under `Project.metadata["pr19_dialogue"]` and is versioned independently from the core Project/Scene schema.

The manifest contains:

- `characters`: project-local `CharacterRecord` values;
- `scenes`: accepted `SceneDialogue` values ordered by canonical project scene order.

Each accepted `SceneDialogue` retains:

- the scene ID;
- the SHA-256 digest of the exact retained PR18 extraction;
- canonical contiguous line order;
- the original OCR region ID;
- the accepted PR18 effective text;
- the source pixel bounding box;
- the project-local speaker ID;
- an optional semantic focus hint.

PR19 does not copy provider order into canonical reading order. Provider order is only review evidence; the accepted ordering is explicit human/workflow authority.

## Character registry boundary

Characters are project-local and use readable `RegistryKey` identifiers. Display names, aliases, and bounded JSON properties may change while the project is otherwise editable.

The registry is frozen while:

- a PR19 dialogue review is open; or
- another blocking review owns the project while it is in `needs_review`.

This prevents an open assignment payload from silently changing meaning between presentation and acceptance.

PR21 may later map these stable narrative IDs to persistent voice-cast identities without changing accepted PR19 dialogue lines.

## Review lifecycle

Preparing one scene requires:

1. the scene still exists;
2. the scene has retained PR18 OCR;
3. all PR18 OCR uncertainty is already resolved;
4. the retained OCR asset still matches the scene's current media asset;
5. at least one character is registered;
6. the scene does not already have accepted dialogue.

Preparation creates the existing canonical `ReviewTask` with:

- `task_type = "dialogue_scene_assignment"`;
- `attention = REVIEW`;
- `priority = HIGH`;
- `blocking = true`;
- a payload reconstructed from retained OCR, the current character registry, and the shared dialogue resume checkpoint.

Multiple panel scenes may hold PR19 review tasks simultaneously. The first task records one project-level resume state in `pr19_dialogue_resume_state`; the checkpoint remains until the last open dialogue task resolves.

Acceptance is intentionally separate from generic PR10 review resolution. The dedicated PR19 path revalidates the exact task authority and reconstructs the canonical review payload from current retained source state before writing dialogue.

If any source identity, review payload, suggestion identity, scene membership, OCR extraction, or character registry evidence has drifted, acceptance fails closed.

## Assisted proposals

`DialogueAssignmentSuggestion` is proposal-only. A proposal may contain:

- reading order;
- per-region speaker IDs;
- an optional focus hint;
- provider label/evidence metadata.

Every proposal must already be a complete, valid assignment over the exact retained OCR region set and known character registry.

Persisted proposals receive deterministic semantic suggestion IDs and are revalidated before queue display or acceptance. Tampered or malformed proposal state quarantines that project's PR19 queue entries rather than poisoning unrelated projects.

The PWA's “Use proposal” action only copies values into the editor. It does not call the acceptance endpoint.

## Focus hints

Focus hints are semantic intent only:

- `speaker`: later choreography may follow the active speaker;
- `face`: one normalized face point;
- `explicit_crop`: one normalized crop rectangle.

PR19 does not compile these hints into motion. PR23 owns concrete camera behavior and must treat the retained hint as input intent rather than renderer geometry.

## Provenance and drift handling

PR19 binds dialogue to PR18 at two levels:

1. retained OCR must still point at the scene's current media asset;
2. accepted dialogue stores the exact PR18 extraction digest.

`DialogueWorkflow.manifest()` rechecks accepted scenes against current retained PR18 state. If OCR or scene-media provenance changes later, PR19 dialogue is not silently reused; callers receive a conflict and an explicit migration/review path is required.

## Concurrency

Project writes use compare-and-swap on the exact persisted Project JSON snapshot. A concurrent mutation therefore cannot be overwritten by a stale PR19 request.

Review acceptance performs source reconstruction and validation on the same snapshot used for the final CAS write.

## HTTP/PWA boundary

PR19 adds authenticated local endpoints for:

- reading the dialogue manifest;
- registering/updating project characters;
- preparing scene assignments;
- listing the canonical dialogue review queue;
- accepting an explicit scene assignment.

Dialogue JSON requests authenticate and enforce content type/body bounds before Pydantic body parsing. Because the PR19 middleware is installed outside the original PR8 middleware, it explicitly preserves the global transport invariant first: plaintext non-loopback requests return `426` before authentication or dialogue-specific body-policy responses.

The PWA dialogue surface provides:

- retained OCR text and source bbox evidence;
- explicit up/down reading-order controls;
- one speaker selector per region;
- proposal-prefill controls with clear non-authority wording;
- optional focus-hint fields;
- one explicit acceptance action.

A richer visual panel editor remains PR22 scope.

## Failure policy

PR19 fails closed for:

- missing/malformed retained OCR;
- unresolved OCR uncertainty;
- scene/media/OCR identity drift;
- incomplete or duplicate reading order;
- incomplete speaker coverage;
- unknown speaker IDs;
- malformed review authority;
- changed canonical review payload;
- tampered assisted suggestions;
- duplicate scene dialogue;
- unsafe project lifecycle state;
- concurrent project mutation.

One malformed project is skipped by the queue reader so independent review work remains available.

## Compatibility

PR19 does not change:

- the core Project or Scene schema;
- PR18 raw/corrected OCR evidence;
- the renderer or RenderPlan contract;
- PR15 provider authority;
- PR17 batch/render/QC behavior.

Future voiced-panel PRs should consume the retained PR19 dialogue manifest rather than inventing a second speaker/order model.
