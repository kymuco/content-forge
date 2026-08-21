# Workflows

## Purpose

Content Forge separates *what a project is* from *how it gets prepared*. Workflows coordinate automatic preparation, bounded review tasks, and rendering readiness without embedding content-specific logic into the renderer.

A workflow should be small, observable, restartable, and explicit about where human judgment is required.

## Project lifecycle

The initial coarse lifecycle is:

```text
INBOX
  |
  v
DRAFT
  |
  v
PREPARED
  |
  +------> NEEDS_REVIEW -----+
  |                          |
  +--------------------------+
  |
  v
READY
  |
  v
RENDERING
  |
  v
QC
  |
  v
DONE
```

The lifecycle is intentionally not overloaded with content-specific states such as `WAITING_FOR_SPEAKER_3`. Fine-grained blockers are represented as `ReviewTask` records.

## Operation classes

Every workflow action is classified as one of:

### `AUTO`

The system is expected to perform the action without asking.

Examples:

- hash an asset;
- run ffprobe;
- generate thumbnail/proxy;
- normalize a known template into a render plan;
- render a preview;
- calculate audio duration;
- detect missing required fields;
- deduplicate identical bytes.

### `REVIEW`

The system prepares a candidate answer or bounded choice, but a human decision is useful or required.

Examples:

- choose one hook from suggestions;
- confirm crop/focus;
- confirm panel order;
- correct low-confidence OCR;
- assign a speaker;
- select a voice;
- confirm an artist credit;
- approve final preview.

### `MANUAL`

The system deliberately leaves control to the operator because automating it is not yet reliable or because the action is inherently creative.

Examples:

- an unusual complex edit not representable by existing components;
- rewriting a scene when the source is ambiguous;
- selecting a source moment when automated suggestions are poor.

The goal is not to eliminate `MANUAL`; it is to prevent accidental manual work from leaking into otherwise deterministic steps.

## Review tasks

A review task is a compact, contextual question.

Example conceptual object:

```yaml
review_task_id: rt_123
project_id: p_184
type: choose_hook
status: open
priority: normal
payload:
  current: null
suggestions:
  - "Her idle animation is way too good"
  - "They didn't have to animate this"
  - "You probably never noticed this"
```

A review task should:

- show the minimum context needed to decide;
- avoid forcing the user into a general editor;
- be solvable from phone when possible;
- preserve both machine suggestion and accepted value;
- never silently re-open or overwrite accepted work after unrelated changes.

## Review queue UX

The queue should prioritize blocking tasks and present project context inline.

Examples:

```text
Choose hook
[preview]
(1) ...
(2) ...
(3) ...
[accept]
```

```text
Confirm panel order
[1] [2] [3] [4]
[drag/reorder]
[accept]
```

```text
OCR confidence low
image crop
raw: "I'II be h0me"
edit: "I'll be home"
[accept]
```

```text
Speaker unknown
line: "Where are you going?"
[Anna] [John] [Narrator] [+ new]
```

## Workflow examples

### `clip_basic`

```text
ingest source                       AUTO
probe media                         AUTO
create source/project record        AUTO
choose trim                         REVIEW or MANUAL
choose fit/crop                     AUTO -> REVIEW if unsafe
render preview                      AUTO
approve preview                     REVIEW
render final                        AUTO
run QC                              AUTO
export                              AUTO
```

### `clip_with_hook`

Adds:

```text
suggest hook candidates             AUTO if LLM configured
choose/edit hook                    REVIEW
```

Without an LLM provider, the hook task becomes a plain text input and the workflow remains valid.

### `art_sequence`

```text
ingest images                       AUTO
preserve upload/order hints         AUTO
confirm/reorder sequence            REVIEW
suggest duration/motion preset      AUTO
confirm source/creator credit       REVIEW when needed
render preview                      AUTO
approve                             REVIEW
render final + QC                   AUTO
```

### `panel_sequence`

Adds readability-oriented review:

```text
panel fit/crop                      AUTO/REVIEW
panel duration                      preset + REVIEW if needed
punchline/final hold                optional REVIEW
music choice                        REVIEW/default
```

### `voiced_dialogue` (later)

```text
OCR extraction                      AUTO
OCR confidence routing              AUTO
correct uncertain text              REVIEW
order dialogue                      AUTO/REVIEW
assign speakers                     REVIEW initially
map speakers to cast                AUTO/REVIEW
synthesize lines                    AUTO
listen/regenerate exceptions        REVIEW
derive durations                    AUTO
build timed text                    AUTO
camera focus hints                  AUTO/REVIEW
render preview                      AUTO
approve                             REVIEW
render final + QC                   AUTO
```

## Workflow dependencies

Actions form a dependency graph, but the implementation should remain simpler than a generic DAG platform.

For example:

```text
asset_ingested
   +-> ffprobe_done
   +-> thumbnail_done
   +-> provenance_recorded

all required source prep
   -> project_prepared

project_prepared + review_tasks_resolved
   -> render_ready
```

Persistent job state belongs in SQLite so desktop/application restarts do not lose work.

## Failure behavior

A failed automatic job should:

1. retain the project and source state;
2. record structured error information;
3. be retryable where safe;
4. not duplicate successful immutable work;
5. surface a clear blocking status when user action is required.

Examples:

- ffprobe failure -> asset marked unprobed, ingest retained;
- preview render failure -> project remains prepared, render job failed;
- LLM unavailable -> suggestion step skipped/fallback input shown;
- TTS generation failure -> only affected line remains unresolved;
- missing template asset -> validation error before expensive render.

## Caching and invalidation

Workflow tasks should reuse intermediates by stable keys.

Examples:

- thumbnail cache: source hash + thumbnail spec;
- proxy cache: source hash + proxy profile;
- preview render: normalized timeline hash + template/component versions + preview profile;
- TTS later: voice ID + text + style/settings + provider/model version;
- OCR later: source hash + OCR provider/model/config.

Changing a hook should not regenerate TTS. Changing an unrelated project tag should not re-render a preview. Changing a source crop should invalidate only dependent render outputs.

## Batch behavior

The queue should support many small projects without making the operator babysit them.

Desired summary:

```text
Today
18 ready automatically
5 need review (~2 min)
2 rendering
1 failed
```

The system should batch deterministic work aggressively while keeping review tasks small and interruptible.

## Publishing boundary

For v0.1, `DONE` means:

- final media exists;
- QC passed or explicit warnings are recorded;
- sidecar metadata exists;
- provenance and render manifest are retained.

Automatic upload/scheduling is deliberately outside the first workflow. Publishing becomes a later provider, not a hidden side effect of rendering.
