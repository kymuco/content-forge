"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const PROJECT_EVENT = "content-forge:project-flow-rendered";
  const PROJECT_CLOSED_EVENT = "content-forge:project-flow-closed";
  const STAGE_ID = "project-publishing-stage";
  let generation = 0;
  let currentCandidate = null;

  function text(tag, value, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value == null ? "" : String(value);
    return node;
  }

  function setStatus(card, message, kind) {
    let node = card.querySelector("[data-project-publishing-status]");
    if (!node) {
      node = text("p", "", "status");
      node.dataset.projectPublishingStatus = "1";
      card.appendChild(node);
    }
    node.textContent = message || "";
    node.dataset.kind = kind || "";
  }

  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }

  async function apiJson(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this phone before using publishing controls.");
    const requestOptions = Object.assign({}, options || {});
    requestOptions.headers = new Headers(requestOptions.headers || {});
    requestOptions.headers.set("Authorization", `Bearer ${bearer}`);
    const response = await fetch(new URL(relativePath, API_BASE), requestOptions);
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return response.json();
  }

  function jsonPost(body) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  function action(label, className, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className || "secondary";
    button.textContent = label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await handler(); }
      finally { button.disabled = false; }
    });
    return button;
  }

  function heading(card, badgeText, badgeClass) {
    const row = document.createElement("div");
    row.className = "card-heading";
    const left = document.createElement("div");
    left.append(
      text("strong", "Publish"),
      text("p", "Separate exact approval after the final render is complete.", "muted compact-text")
    );
    row.append(left, text("span", badgeText, badgeClass || "badge neutral"));
    card.appendChild(row);
  }

  function projectMetadata(project) {
    const tasks = Array.isArray(project.tasks) ? project.tasks : [];
    const task = tasks.find((item) => item && item.task_type === "metadata") || null;
    let source = null;
    if (task && task.accepted_value && typeof task.accepted_value === "object") {
      source = task.accepted_value;
    } else if (task && task.payload && typeof task.payload === "object") {
      source = task.payload;
    }
    return {
      title: source && typeof source.title === "string" && source.title.trim()
        ? source.title.trim()
        : (project.production_preset_label || "Video"),
      description: source && typeof source.description === "string" ? source.description : "",
      tags: source && Array.isArray(source.hashtags)
        ? source.hashtags.filter((value) => typeof value === "string" && value.trim()).map((value) => value.trim())
        : [],
    };
  }

  function parseTags(value) {
    const result = [];
    const seen = new Set();
    for (const raw of String(value || "").split(/[\n,]/)) {
      const tag = raw.trim();
      if (!tag) continue;
      const identity = tag.toLocaleLowerCase();
      if (seen.has(identity)) throw new Error(`Duplicate publish tag: ${tag}`);
      seen.add(identity);
      result.push(tag);
    }
    if (result.length > 64) throw new Error("Publish metadata supports at most 64 tags.");
    return result;
  }

  function declarationSelect(labelText, helpText) {
    const label = document.createElement("label");
    label.textContent = labelText;
    const select = document.createElement("select");
    const choose = document.createElement("option");
    choose.value = "";
    choose.textContent = "Choose…";
    const no = document.createElement("option");
    no.value = "no";
    no.textContent = "No";
    const yes = document.createElement("option");
    yes.value = "yes";
    yes.textContent = "Yes";
    select.append(choose, no, yes);
    label.appendChild(select);
    if (helpText) label.appendChild(text("span", helpText, "muted compact-text"));
    return { label, select };
  }

  function requiredDeclaration(select, label) {
    if (select.value !== "yes" && select.value !== "no") {
      throw new Error(`${label} must be explicitly answered Yes or No.`);
    }
    return select.value === "yes";
  }

  function scheduleValue(input) {
    const value = input.value.trim();
    if (!value) return null;
    const instant = new Date(value);
    if (Number.isNaN(instant.getTime())) throw new Error("Schedule time is invalid.");
    return instant.toISOString();
  }

  function exactCurrentFinalAttempts(context, project) {
    const final = project.final || {};
    const items = Array.isArray(context.items) ? context.items : [];
    return items.filter((item) => {
      const request = item && item.request;
      const artifact = request && request.artifact;
      return artifact
        && artifact.project_id === project.project_id
        && artifact.render_job_id === final.job_id
        && artifact.output_sha256 === final.output_sha256;
    });
  }

  function attemptState(item) {
    return String(item && item.attempt && item.attempt.state || "").toLowerCase();
  }

  function statePriority(state) {
    if (state === "succeeded") return 5;
    if (state === "outcome_unknown") return 4;
    if (state === "running") return 3;
    if (state === "prepared") return 2;
    if (state === "failed") return 1;
    return 0;
  }

  function strongestAttempt(items) {
    let selected = null;
    for (const item of items) {
      if (!selected || statePriority(attemptState(item)) > statePriority(attemptState(selected))) {
        selected = item;
      }
    }
    return selected;
  }

  function drawReceipt(card, payload) {
    const attempt = payload.attempt || {};
    const request = payload.request || {};
    const target = request.target || {};
    const result = attempt.result || null;
    card.appendChild(text("p", `Destination: ${target.provider_id || ""} / ${target.destination_id || ""}`, "compact-text"));
    card.appendChild(text("p", `Request: ${payload.request_sha256 || ""}`, "mono wrap compact-text"));
    if (attempt.state === "succeeded" && result) {
      card.appendChild(text("p", `Published remote ID: ${result.remote_id || ""}`, "compact-text"));
      if (result.remote_url) {
        const link = document.createElement("a");
        link.href = result.remote_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Open published video";
        card.appendChild(link);
      }
    }
  }

  function advancedFallback(project, defaults) {
    return action("Open Advanced publishing", "secondary", async () => {
      const advanced = document.getElementById("production-home-advanced");
      if (advanced && advanced.getAttribute("aria-expanded") !== "true") advanced.click();
      const renderJob = document.getElementById("publishing-render-job");
      const title = document.getElementById("publishing-title");
      const description = document.getElementById("publishing-description");
      const tags = document.getElementById("publishing-tags");
      if (renderJob) renderJob.value = project.final && project.final.job_id || "";
      if (title && !title.value) title.value = defaults.title;
      if (description && !description.value) description.value = defaults.description;
      if (tags && !tags.value) tags.value = defaults.tags.join(", ");
      const panel = document.getElementById("publishing-panel");
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function renderExistingAttempt(card, context, project, payload, defaults, rerender) {
    const attempt = payload.attempt || {};
    const state = attempt.state;
    card.replaceChildren();
    heading(
      card,
      state === "succeeded" ? "Published" : state,
      state === "succeeded" ? "badge success" : (state === "outcome_unknown" ? "badge state-failed" : "badge neutral")
    );
    drawReceipt(card, payload);

    if (state === "succeeded") {
      setStatus(card, "This exact final already has a durable successful publication receipt.", "success");
      return;
    }
    if (state === "outcome_unknown") {
      setStatus(card, "Remote outcome is unknown. Routine replacement publishing is blocked to prevent a duplicate upload.", "error");
      card.appendChild(advancedFallback(project, defaults));
      return;
    }
    if (state === "running") {
      setStatus(card, "Remote execution may be in progress. Do not create a replacement request.");
      card.appendChild(action("Refresh publish state", "secondary", rerender));
      return;
    }
    if (state === "prepared") {
      setStatus(card, context.provider_configured
        ? "Exact request is human-approved. Remote execution has not begun."
        : "Exact request is approved locally. Configure the publishing provider on the desktop before execution.");
      if (context.provider_configured) {
        card.appendChild(action("Execute approved upload", "primary", async () => {
          setStatus(card, "Executing the already-approved publish request…");
          try {
            await apiJson(
              `publishing/attempts/${encodeURIComponent(attempt.attempt_id)}/execute`,
              { method: "POST" }
            );
          } finally {
            await rerender();
          }
        }));
      } else {
        card.appendChild(advancedFallback(project, defaults));
      }
      return;
    }
    if (state === "failed") {
      setStatus(card, attempt.error_message || "Previous publish attempt failed before a trusted remote result was recorded.", "error");
      card.appendChild(action("Prepare another exact request", "secondary", rerender));
    }
  }

  function verifyCandidate(candidate, project, target) {
    const request = candidate && candidate.request;
    const artifact = request && request.artifact;
    const actualTarget = request && request.target;
    const final = project.final || {};
    if (!request || request.contract_version !== "pr29_publish_contract_v2") {
      throw new Error("Publishing candidate did not preserve the required v2 contract.");
    }
    if (!artifact
        || artifact.project_id !== project.project_id
        || artifact.render_job_id !== final.job_id
        || artifact.output_sha256 !== final.output_sha256) {
      throw new Error("Publishing candidate does not match the exact current final video.");
    }
    if (!actualTarget
        || actualTarget.provider_id !== target.provider_id
        || actualTarget.destination_id !== target.destination_id) {
      throw new Error("Publishing candidate destination changed unexpectedly.");
    }
  }

  function renderCandidate(card, candidate, project, context, defaults, rerender) {
    card.replaceChildren();
    heading(card, "Review", "badge state-partial");
    const request = candidate.request || {};
    const metadata = request.metadata || {};
    const declarations = request.declarations || {};
    card.appendChild(text("p", metadata.title || "", "compact-text"));
    card.appendChild(text("p", `Visibility: ${metadata.visibility || ""}${metadata.scheduled_for ? ` · ${metadata.scheduled_for}` : ""}`, "compact-text"));
    card.appendChild(text("p", `Made for kids: ${declarations.child_directed === true ? "Yes" : "No"}`, "compact-text"));
    card.appendChild(text("p", `Realistic altered/synthetic: ${declarations.contains_realistic_altered_or_synthetic_media === true ? "Yes" : "No"}`, "compact-text"));
    card.appendChild(text("p", `Exact final SHA-256: ${request.artifact && request.artifact.output_sha256 || ""}`, "mono wrap compact-text"));
    card.appendChild(text("p", `Exact request SHA-256: ${candidate.request_sha256 || ""}`, "mono wrap compact-text"));
    setStatus(card, "Review this exact request. Approval is separate from remote execution.");
    const actions = document.createElement("div");
    actions.className = "review-actions";
    actions.appendChild(action("Approve exact publish request", "primary", async () => {
      if (currentCandidate !== candidate) throw new Error("Publish candidate is no longer current.");
      setStatus(card, "Persisting exact human approval…");
      await apiJson("publishing/attempts", jsonPost({
        request: candidate.request,
        confirm_request_sha256: candidate.request_sha256,
      }));
      currentCandidate = null;
      await rerender();
    }));
    actions.appendChild(action("Edit publish details", "ghost", async () => {
      currentCandidate = null;
      await renderStage(project, ++generation, card);
    }));
    card.appendChild(actions);
  }

  function renderForm(card, context, project, defaults, rerender) {
    const target = context.configured_target;
    if (!target) {
      card.replaceChildren();
      heading(card, context.provider_configured ? "Setup needed" : "Optional", "badge neutral");
      setStatus(card, context.provider_configured
        ? "The configured provider does not expose a safe credential-free destination to the phone flow."
        : "No remote publishing provider is configured. The final video remains fully usable locally.");
      card.appendChild(advancedFallback(project, defaults));
      return;
    }

    card.replaceChildren();
    heading(card, target.provider_id === "youtube" ? "YouTube" : "Publish", "badge neutral");
    card.appendChild(text("p", `Destination: ${target.provider_id} / ${target.destination_id}`, "muted compact-text"));
    const form = document.createElement("form");
    form.className = "stack compact";

    const titleLabel = document.createElement("label");
    titleLabel.textContent = "Title";
    const titleInput = document.createElement("input");
    titleInput.required = true;
    titleInput.maxLength = target.provider_id === "youtube" ? 100 : 500;
    titleInput.value = defaults.title;
    titleLabel.appendChild(titleInput);

    const descriptionLabel = document.createElement("label");
    descriptionLabel.textContent = "Description";
    const descriptionInput = document.createElement("textarea");
    descriptionInput.rows = 4;
    descriptionInput.maxLength = 20000;
    descriptionInput.value = defaults.description;
    descriptionLabel.appendChild(descriptionInput);

    const tagsLabel = document.createElement("label");
    tagsLabel.textContent = "Tags (comma separated)";
    const tagsInput = document.createElement("input");
    tagsInput.maxLength = 4096;
    tagsInput.value = defaults.tags.join(", ");
    tagsLabel.appendChild(tagsInput);

    const visibilityLabel = document.createElement("label");
    visibilityLabel.textContent = "Visibility";
    const visibility = document.createElement("select");
    for (const value of ["private", "unlisted", "public"]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value.charAt(0).toUpperCase() + value.slice(1);
      if (value === "private") option.selected = true;
      visibility.appendChild(option);
    }
    visibilityLabel.appendChild(visibility);

    const scheduleLabel = document.createElement("label");
    scheduleLabel.textContent = "Schedule (optional)";
    const schedule = document.createElement("input");
    schedule.type = "datetime-local";
    scheduleLabel.appendChild(schedule);

    const childDirected = declarationSelect(
      "Made for kids / child-directed?",
      "Explicit publication declaration; Content Forge does not infer it."
    );
    const synthetic = declarationSelect(
      "Realistic altered or synthetic media?",
      "Explicit publication declaration; Content Forge does not classify it automatically."
    );

    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "primary";
    submit.textContent = "Build exact publish request";
    form.append(
      titleLabel,
      descriptionLabel,
      tagsLabel,
      visibilityLabel,
      scheduleLabel,
      childDirected.label,
      synthetic.label,
      submit
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const scheduledFor = scheduleValue(schedule);
        if (scheduledFor && target.provider_id === "youtube" && visibility.value !== "public") {
          throw new Error("Scheduled YouTube publishing requires Public visibility.");
        }
        setStatus(card, "Authenticating the exact final and building publish request…");
        const metadata = {
          title: titleInput.value.trim(),
          description: descriptionInput.value,
          tags: parseTags(tagsInput.value),
          visibility: visibility.value,
        };
        if (!metadata.title) throw new Error("Publish title is required.");
        if (scheduledFor) metadata.scheduled_for = scheduledFor;
        const candidate = await apiJson("publishing/candidates", jsonPost({
          render_job_id: project.final.job_id,
          target,
          metadata,
          contract_version: "pr29_publish_contract_v2",
          declarations: {
            child_directed: requiredDeclaration(childDirected.select, "Made-for-kids declaration"),
            contains_realistic_altered_or_synthetic_media: requiredDeclaration(
              synthetic.select,
              "Altered/synthetic-media declaration"
            ),
          },
        }));
        verifyCandidate(candidate, project, target);
        currentCandidate = candidate;
        renderCandidate(card, candidate, project, context, defaults, rerender);
      } catch (error) {
        currentCandidate = null;
        setStatus(card, error && error.message ? error.message : "Publish request could not be built.", "error");
      } finally {
        submit.disabled = false;
      }
    });
    card.appendChild(form);
    setStatus(card, "Nothing remote happens until you build, approve, and then explicitly execute the exact request.");
  }

  async function renderStage(project, myGeneration, existingCard) {
    if (!project || String(project.state || "").toUpperCase() !== "DONE") return;
    const final = project.final;
    if (!final || typeof final.job_id !== "string" || typeof final.output_sha256 !== "string") return;
    const body = document.getElementById("project-flow-body");
    if (!body) return;

    currentCandidate = null;
    const card = existingCard || document.createElement("article");
    card.id = STAGE_ID;
    card.className = "review-card";
    if (!existingCard) body.appendChild(card);
    card.replaceChildren();
    heading(card, "Loading", "badge neutral");
    setStatus(card, "Loading durable publishing state…");

    try {
      // Status is intentionally read first. PR27 performs one-time interrupted-attempt
      // reconciliation there, so the project projection cannot display stale running
      // state after a desktop restart.
      await apiJson("publishing/status");
      const context = await apiJson(
        `publishing/projects/${encodeURIComponent(project.project_id)}?limit=50`
      );
      if (myGeneration !== generation) return;
      if (context.project_id !== project.project_id) {
        throw new Error("Publishing context returned a different project identity.");
      }
      const defaults = projectMetadata(project);
      const attempts = exactCurrentFinalAttempts(context, project);
      const active = strongestAttempt(attempts);
      const preparedCount = attempts.filter((item) => attemptState(item) === "prepared").length;
      const runningCount = attempts.filter((item) => attemptState(item) === "running").length;
      const rerender = async () => renderStage(project, generation, card);

      if (preparedCount > 1 || runningCount > 1) {
        card.replaceChildren();
        heading(card, "Reconcile", "badge state-failed");
        setStatus(card, "Multiple active publish attempts exist for this exact final. Routine execution is blocked until they are reconciled.", "error");
        card.appendChild(advancedFallback(project, defaults));
        return;
      }
      if (active && attemptState(active) !== "failed") {
        renderExistingAttempt(card, context, project, active, defaults, rerender);
        return;
      }
      if (active && attemptState(active) === "failed") {
        card.replaceChildren();
        heading(card, "Retry safe", "badge state-partial");
        setStatus(card, active.attempt && active.attempt.error_message || "Previous attempt failed before a trusted remote result.", "error");
        card.appendChild(action("Create a new exact request", "secondary", async () => {
          renderForm(card, context, project, defaults, rerender);
        }));
        return;
      }
      renderForm(card, context, project, defaults, rerender);
    } catch (error) {
      if (myGeneration !== generation) return;
      card.replaceChildren();
      heading(card, "Unavailable", "badge state-failed");
      setStatus(card, error && error.message ? error.message : "Publishing state could not be loaded.", "error");
    }
  }

  window.addEventListener(PROJECT_EVENT, (event) => {
    const project = event && event.detail && event.detail.project;
    const myGeneration = ++generation;
    const existing = document.getElementById(STAGE_ID);
    if (existing) existing.remove();
    if (project && String(project.state || "").toUpperCase() === "DONE") {
      void renderStage(project, myGeneration, null);
    }
  });

  window.addEventListener(PROJECT_CLOSED_EVENT, () => {
    generation += 1;
    currentCandidate = null;
    const existing = document.getElementById(STAGE_ID);
    if (existing) existing.remove();
  });
})();
