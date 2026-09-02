# PR33 — Project-specific phone edit, preview, and final flow

PR33 turns the PR31/PR32 phone production surface into one coherent project-specific workflow from bounded review decisions through authenticated preview approval and final output.

It does **not** add a second Project model, review state machine, render queue, or artifact authority. The phone surface is a projection over the existing authenticated Project summary and the existing Review/Preview/Final endpoints.

## Product flow

```text
Production Home
-> open one Project
-> make only the review decisions modeled for that Project
-> generate authenticated preview
-> watch preview on phone
-> approve OR reject and edit in the same Project context
-> render final on the desktop worker
-> observe render/QC state
-> watch authenticated final on phone
```

A Project created by the PR32 `Create video` wizard opens directly into this flow. Existing projects use the same surface through `Start video`, `Continue`, `View progress`, or `View final` from Production Home.

## Authority boundary

PR33 does not introduce a new project-flow endpoint. It reads:

```text
GET /api/v1/projects/{project_id}
```

and uses the already-established mutations:

```text
POST /api/v1/projects/{project_id}/review/bootstrap
POST /api/v1/projects/{project_id}/review/{task_id}/resolve
POST /api/v1/projects/{project_id}/preview
POST /api/v1/projects/{project_id}/preview/{job_id}/approve
POST /api/v1/projects/{project_id}/preview/{job_id}/reject
POST /api/v1/projects/{project_id}/final
GET  /api/v1/render-jobs/{job_id}/artifact
```

The active `project_id` is therefore UI navigation state only. It does not become durable product authority and is never accepted as evidence for a semantic decision.

All mutations continue through bearer-authenticated, fail-closed Review/Render contracts. Artifact playback remains authenticated and does not expose runtime filesystem paths.

## Project-specific decisions

The phone editor exposes only decisions already modeled by the canonical Review task contract.

### Hook

An open `hook` task exposes one bounded text editor. Saving resolves the exact current review task through the existing endpoint. The server remains authoritative for non-empty content and the 4096-character limit.

### Crop

An open `crop_confirmation` task exposes one editor per current scene:

- `Use full frame`, represented by `null`; or
- normalized `x`, `y`, `width`, `height` values.

The phone performs an early bounded check that coordinates remain inside `0..1` and that the rectangle fits inside the frame. The server still performs authoritative `NormalizedRect` validation and exact scene coverage validation.

The mobile layout uses a 2×2 coordinate grid on narrow screens and expands to four columns only when sufficient width is available.

### Video details

An open `metadata` task exposes optional title, description, and comma-separated hashtags. The client uses the existing server-aligned title/description bounds and submits the same exact metadata object accepted by the Review service. Server validation remains authoritative for field shape, hashtag count, and per-tag length.

### Source order

PR32 source order is **not** reopened in PR33. The order was already an explicit human choice in `Create video` and is frozen into `production_preset_v1` evidence.

The project screen shows that retained order as read-only source positions. A future reorder feature would require an intentional new authority contract; PR33 does not create a second mutation path that could contradict PR32 evidence.

### Unsupported/manual decisions

A task outside the bounded PR33 editor is shown as read-only. If the task is still actionable and the Project lifecycle permits it, the user can open the existing Advanced review surface. PR33 does not guess how an unknown or MANUAL task should be resolved.

## Preview lifecycle

Preview remains the existing low-resolution Review artifact, not a new phone-specific render type.

Before preview generation, all blocking non-preview review decisions must be resolved. The Project screen then calls the existing preview endpoint.

When the current preview candidate is ready:

- its authenticated artifact is loaded inline;
- `Approve preview` calls the exact existing approval endpoint for that `job_id`;
- `Reject & edit` calls the exact existing rejection endpoint and keeps the user in the same Project context.

Rejecting reopens the existing editable Review decisions according to core semantics. PR33 does not reconstruct or locally simulate that state.

Approval remains bound by the existing render-plan and project-revision identity checks. A stale preview cannot be converted into final-render authority by the UI.

## READY edits and terminal-state locking

The Review core intentionally treats `READY` differently from final execution states.

While a Project is `READY`, an optional open review decision may still be edited. The existing Review service then invalidates the approved preview and returns the Project to `NEEDS_REVIEW`, forcing a fresh preview before final render. PR33 preserves that behavior instead of inventing an additional edit mode.

`RENDERING`, `QC`, and `DONE` are terminal for phone review mutations. A historically open optional task may still exist in the Project manifest, but PR33 renders it as **Locked** history and exposes no Save action. This prevents the phone from presenting a mutation that core authority would correctly reject.

## Final lifecycle

Final rendering is available only when the canonical Project is `READY` with a valid approved-preview identity.

The Project screen then reflects existing lifecycle states:

- `READY` — explicit `Render final` action;
- `RENDERING` / `QC` — no mutation, only progress/refresh;
- `DONE` — authenticated final artifact playback inline.

Final rendering, QC, restart recovery, render-plan identity, artifact hashes, and corruption handling remain fully owned by the existing Review/Render runtime.

PR33 does not create a second progress ledger or infer success from UI state.

## Production Home behavior

Production Home remains the project list/entry surface. It no longer uses separate final/watch shortcuts that bypass project context.

Normal actions open the project-specific flow:

- `Start video` bootstraps then opens the Project;
- `Continue` opens the Project;
- `View progress` opens the Project;
- `View final` opens the Project.

A newly created PR32 production Project opens immediately after successful creation.

Global Capture and Review surfaces are retained, together with the existing specialist production panels, behind `Advanced`. They remain fallback/debug controls rather than ordinary daily navigation.

## Phone/PWA behavior

The project flow is constructed inside the existing PWA controller and keeps the existing CSP-safe DOM approach. No `innerHTML` rendering is introduced.

Preview and final media use authenticated fetches and short-lived browser object URLs. Object URLs are revoked when the active project changes, the Home surface is restored, or the page unloads.

The installed PWA shell advances from v18 to v19. The PR32 v18 cache remains an explicit predecessor so installed clients upgrade deterministically.

## Failure and refresh behavior

The Project screen always reloads canonical Project state from the server after a mutation. It does not optimistically persist its own review/render lifecycle.

If a request fails, the user sees the bounded API error on the current Project screen. Refresh/focus/visibility refreshes reload the active Project rather than silently returning to Home.

Render and preview restart semantics remain those already implemented by the durable Review/Render runtime.

## Security properties retained

PR33 preserves the established boundaries:

- paired bearer authentication for project/review/render access;
- no raw filesystem paths in phone APIs;
- no provider credentials in the project flow;
- no automatic human-review acceptance;
- exact preview approval before final authority;
- authenticated preview/final artifact reads;
- no publishing side effect from render approval;
- no new browser-side durable semantic authority.

## Non-goals

PR33 does not add:

- a general-purpose mobile NLE or timeline editor;
- a second Project/review/render state machine;
- source reordering after PR32 Project creation;
- new render profiles or FFmpeg semantics;
- publishing handoff or destination selection;
- batch attention handling;
- analytics or recommendation logic.

PR34 owns the exact final-to-publish phone handoff. PR35 owns the daily batch Inbox/attention experience.
