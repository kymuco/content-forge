"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("publishing-panel");
  const providerBadge = document.getElementById("publishing-provider-badge");
  const form = document.getElementById("publishing-candidate-form");
  const renderJobInput = document.getElementById("publishing-render-job");
  const providerInput = document.getElementById("publishing-provider-id");
  const destinationInput = document.getElementById("publishing-destination-id");
  const titleInput = document.getElementById("publishing-title");
  const descriptionInput = document.getElementById("publishing-description");
  const tagsInput = document.getElementById("publishing-tags");
  const visibilityInput = document.getElementById("publishing-visibility");
  const scheduleInput = document.getElementById("publishing-schedule");
  const candidateView = document.getElementById("publishing-candidate-view");
  const approveButton = document.getElementById("publishing-approve");
  const approvalNote = document.getElementById("publishing-approval-note");
  const attemptInput = document.getElementById("publishing-attempt-id");
  const loadAttemptButton = document.getElementById("publishing-load-attempt");
  const executeButton = document.getElementById("publishing-execute");
  const attemptView = document.getElementById("publishing-attempt-view");
  const status = document.getElementById("publishing-status");

  if (!panel || !providerBadge || !form || !renderJobInput || !providerInput
      || !destinationInput || !titleInput || !descriptionInput || !tagsInput
      || !visibilityInput || !scheduleInput || !candidateView || !approveButton
      || !approvalNote || !attemptInput || !loadAttemptButton || !executeButton
      || !attemptView || !status) return;

  function addRequiredDeclarationSelect(id, labelText, helpText) {
    let select = document.getElementById(id);
    if (select) return select;
    const label = document.createElement("label");
    label.textContent = labelText;
    select = document.createElement("select");
    select.id = id;
    select.required = true;
    const choose = document.createElement("option");
    choose.value = "";
    choose.textContent = "Choose…";
    choose.selected = true;
    const no = document.createElement("option");
    no.value = "no";
    no.textContent = "No";
    const yes = document.createElement("option");
    yes.value = "yes";
    yes.textContent = "Yes";
    select.append(choose, no, yes);
    label.appendChild(select);
    if (helpText) {
      const help = document.createElement("span");
      help.className = "muted compact-text";
      help.textContent = helpText;
      label.appendChild(help);
    }
    const scheduleLabel = scheduleInput.closest("label");
    if (scheduleLabel) scheduleLabel.insertAdjacentElement("afterend", label);
    else form.appendChild(label);
    return select;
  }

  const syntheticInput = addRequiredDeclarationSelect(
    "publishing-synthetic-media",
    "Realistic altered or synthetic media?",
    "Choose Yes only when realistic content was meaningfully altered or synthetically generated."
  );
  const childDirectedInput = addRequiredDeclarationSelect(
    "publishing-child-directed",
    "Made for kids / child-directed?",
    "This is an explicit publication declaration, not an inferred audience guess."
  );

  let currentCandidate = null;
  let providerConfigured = false;
  let loadedAttemptId = null;

  function setStatus(message, kind) {
    status.textContent = message || "";
    status.dataset.kind = kind || "";
  }

  function setHidden(element, hidden) {
    element.classList.toggle("hidden", Boolean(hidden));
  }

  function text(tag, value, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value == null ? "" : String(value);
    return node;
  }

  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }

  async function apiJson(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this device before using publishing controls.");
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
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function jsonRequest(method, body) {
    return {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  function parseTags(value) {
    const tags = [];
    const seen = new Set();
    for (const raw of String(value || "").split(/[\n,]/)) {
      const tag = raw.trim();
      if (!tag) continue;
      const identity = tag.toLocaleLowerCase();
      if (seen.has(identity)) throw new Error(`Duplicate publish tag: ${tag}`);
      seen.add(identity);
      tags.push(tag);
    }
    if (tags.length > 64) throw new Error("Publish metadata supports at most 64 tags.");
    return tags;
  }

  function scheduleIso() {
    const value = scheduleInput.value.trim();
    if (!value) return null;
    const instant = new Date(value);
    if (Number.isNaN(instant.getTime())) throw new Error("Schedule time is invalid.");
    return instant.toISOString();
  }

  function declarationValue(input, label) {
    if (input.value !== "yes" && input.value !== "no") {
      throw new Error(`${label} must be explicitly answered Yes or No.`);
    }
    return input.value === "yes";
  }

  function candidatePayload() {
    const renderJobId = renderJobInput.value.trim();
    const providerId = providerInput.value.trim();
    const destinationId = destinationInput.value.trim();
    const title = titleInput.value.trim();
    if (!renderJobId || !providerId || !destinationId || !title) {
      throw new Error("Final render job, provider, destination, and title are required.");
    }
    const metadata = {
      title,
      description: descriptionInput.value,
      tags: parseTags(tagsInput.value),
      visibility: visibilityInput.value,
    };
    const scheduledFor = scheduleIso();
    if (scheduledFor) metadata.scheduled_for = scheduledFor;
    return {
      render_job_id: renderJobId,
      target: { provider_id: providerId, destination_id: destinationId },
      metadata,
      contract_version: "pr29_publish_contract_v2",
      declarations: {
        child_directed: declarationValue(childDirectedInput, "Made-for-kids declaration"),
        contains_realistic_altered_or_synthetic_media: declarationValue(
          syntheticInput,
          "Altered/synthetic-media declaration"
        ),
      },
    };
  }

  function invalidateCandidate() {
    if (currentCandidate) {
      currentCandidate = null;
      candidateView.replaceChildren(
        text("p", "Candidate invalidated because publishing inputs changed. Build it again before approval.", "status")
      );
    }
    approveButton.disabled = true;
  }

  function invalidateLoadedAttempt() {
    if (loadedAttemptId && attemptInput.value.trim() === loadedAttemptId) return;
    loadedAttemptId = null;
    executeButton.disabled = true;
    attemptView.replaceChildren();
  }

  function declarationSummary(request) {
    const declarations = request && request.declarations ? request.declarations : null;
    if (!declarations) return "legacy v1 declarations: not present";
    const kids = declarations.child_directed === true ? "yes" : "no";
    const synthetic = declarations.contains_realistic_altered_or_synthetic_media === true ? "yes" : "no";
    return `made for kids: ${kids} · realistic altered/synthetic: ${synthetic}`;
  }

  function drawCandidate(candidate) {
    candidateView.replaceChildren();
    const request = candidate.request || {};
    const artifact = request.artifact || {};
    const target = request.target || {};
    const metadata = request.metadata || {};
    const card = document.createElement("article");
    card.className = "review-card";
    card.appendChild(text("strong", "Exact publish candidate"));
    card.appendChild(text("p", `contract: ${request.contract_version || "pr27_publish_contract_v1"}`, "mono wrap compact-text"));
    card.appendChild(text("p", `request SHA-256: ${candidate.request_sha256}`, "mono wrap compact-text"));
    card.appendChild(text("p", `idempotency: ${candidate.idempotency_key}`, "mono wrap compact-text"));
    card.appendChild(text("p", `final artifact: ${artifact.output_sha256 || ""}`, "mono wrap compact-text"));
    card.appendChild(text("p", `render job: ${artifact.render_job_id || ""}`, "mono wrap compact-text"));
    card.appendChild(text("p", `destination: ${target.provider_id || ""} / ${target.destination_id || ""}`, "compact-text"));
    card.appendChild(text("p", `visibility: ${metadata.visibility || ""}${metadata.scheduled_for ? ` · scheduled ${metadata.scheduled_for}` : " · publish now"}`, "compact-text"));
    card.appendChild(text("p", declarationSummary(request), "compact-text"));
    card.appendChild(text("p", metadata.title || "", "compact-text"));
    if (metadata.description) card.appendChild(text("p", metadata.description, "muted compact-text wrap"));
    if (Array.isArray(metadata.tags) && metadata.tags.length) {
      card.appendChild(text("p", `tags: ${metadata.tags.join(", ")}`, "muted compact-text wrap"));
    }
    candidateView.appendChild(card);
    approveButton.disabled = false;
  }

  function stateBadgeClass(state) {
    if (state === "succeeded") return "badge success";
    if (state === "failed" || state === "outcome_unknown") return "badge danger";
    return "badge neutral";
  }

  function drawAttempt(payload) {
    attemptView.replaceChildren();
    const attempt = payload && payload.attempt ? payload.attempt : {};
    const request = payload && payload.request ? payload.request : {};
    const target = request.target || {};
    const result = attempt.result || null;
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.appendChild(text("strong", attempt.attempt_id || "Publish attempt"));
    left.appendChild(text("p", payload.request_sha256 || "", "mono wrap compact-text muted"));
    heading.appendChild(left);
    heading.appendChild(text("span", attempt.state || "unknown", stateBadgeClass(attempt.state)));
    card.appendChild(heading);
    card.appendChild(text("p", `contract: ${request.contract_version || "pr27_publish_contract_v1"}`, "mono wrap compact-text"));
    card.appendChild(text("p", declarationSummary(request), "compact-text"));
    card.appendChild(text("p", `destination: ${target.provider_id || ""} / ${target.destination_id || ""}`, "compact-text"));
    card.appendChild(text("p", `idempotency: ${payload.idempotency_key || ""}`, "mono wrap compact-text"));
    if (attempt.state === "prepared") {
      card.appendChild(text("p", "Human approval is durable. Remote execution has not begun.", "status"));
    } else if (attempt.state === "running") {
      card.appendChild(text("p", "Remote execution may be in progress. Do not create a replacement attempt.", "status"));
    } else if (attempt.state === "outcome_unknown") {
      card.appendChild(text("p", "Remote outcome is unknown. Automatic retry is blocked to prevent duplicate publication.", "status"));
    } else if (attempt.state === "failed") {
      card.appendChild(text("p", attempt.error_message || "Publish preflight failed without a remote side effect.", "status"));
    } else if (attempt.state === "succeeded" && result) {
      card.appendChild(text("p", `remote ID: ${result.remote_id || ""}`, "mono wrap compact-text"));
      if (result.remote_url) card.appendChild(text("p", result.remote_url, "mono wrap compact-text"));
    }
    attemptView.appendChild(card);
    loadedAttemptId = typeof attempt.attempt_id === "string" && attempt.attempt_id
      ? attempt.attempt_id
      : null;
    if (loadedAttemptId) attemptInput.value = loadedAttemptId;
    executeButton.disabled = !loadedAttemptId || attempt.state !== "prepared" || !providerConfigured;
  }

  async function refreshProviderStatus() {
    const payload = await apiJson("publishing/status");
    providerConfigured = Boolean(payload.provider_configured);
    providerBadge.textContent = providerConfigured ? "Provider ready" : "Approval only";
    providerBadge.className = providerConfigured ? "badge success" : "badge neutral";
    if (!providerConfigured) {
      executeButton.disabled = true;
    } else if (attemptInput.value.trim()) {
      try {
        const payload = await apiJson(`publishing/attempts/${encodeURIComponent(attemptInput.value.trim())}`);
        drawAttempt(payload);
      } catch (_) {}
    }
  }

  form.addEventListener("input", invalidateCandidate);
  form.addEventListener("change", invalidateCandidate);
  attemptInput.addEventListener("input", invalidateLoadedAttempt);
  attemptInput.addEventListener("change", invalidateLoadedAttempt);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    approveButton.disabled = true;
    try {
      setStatus("Authenticating final render and building publish candidate…");
      const candidate = await apiJson(
        "publishing/candidates",
        jsonRequest("POST", candidatePayload())
      );
      currentCandidate = candidate;
      providerConfigured = Boolean(candidate.provider_configured);
      providerBadge.textContent = providerConfigured ? "Provider ready" : "Approval only";
      providerBadge.className = providerConfigured ? "badge success" : "badge neutral";
      drawCandidate(candidate);
      setStatus("Candidate built. Review its exact digest and declarations before approving.", "success");
    } catch (error) {
      currentCandidate = null;
      candidateView.replaceChildren();
      setStatus(error.message || "Publish candidate could not be built.", "error");
    }
  });

  approveButton.addEventListener("click", async () => {
    if (!currentCandidate) {
      setStatus("Build the exact candidate again before approval.", "error");
      return;
    }
    approveButton.disabled = true;
    try {
      const payload = {
        request: currentCandidate.request,
        confirm_request_sha256: currentCandidate.request_sha256,
      };
      const note = approvalNote.value.trim();
      if (note) payload.note = note;
      const approved = await apiJson(
        "publishing/attempts",
        jsonRequest("POST", payload)
      );
      drawAttempt(approved);
      setStatus("Exact publish request and declarations approved and stored. Remote execution has not started.", "success");
    } catch (error) {
      setStatus(error.message || "Publish approval failed.", "error");
    } finally {
      approveButton.disabled = currentCandidate == null;
    }
  });

  loadAttemptButton.addEventListener("click", async () => {
    const attemptId = attemptInput.value.trim();
    if (!attemptId) {
      setStatus("Publish attempt ID is required.", "error");
      return;
    }
    loadAttemptButton.disabled = true;
    executeButton.disabled = true;
    loadedAttemptId = null;
    try {
      const payload = await apiJson(`publishing/attempts/${encodeURIComponent(attemptId)}`);
      drawAttempt(payload);
      setStatus(`Loaded ${payload.attempt.state} publish attempt.`, "success");
    } catch (error) {
      attemptView.replaceChildren();
      executeButton.disabled = true;
      setStatus(error.message || "Publish attempt could not be loaded.", "error");
    } finally {
      loadAttemptButton.disabled = false;
    }
  });

  executeButton.addEventListener("click", async () => {
    const attemptId = loadedAttemptId;
    if (!attemptId || attemptInput.value.trim() !== attemptId) {
      invalidateLoadedAttempt();
      setStatus("Load the exact publish attempt before execution.", "error");
      return;
    }
    executeButton.disabled = true;
    try {
      setStatus("Executing the already-approved publish attempt…");
      const payload = await apiJson(
        `publishing/attempts/${encodeURIComponent(attemptId)}/execute`,
        { method: "POST" }
      );
      drawAttempt(payload);
      setStatus(
        payload.attempt.state === "succeeded"
          ? "Publish receipt authenticated and stored."
          : `Publish attempt is ${payload.attempt.state}.`,
        payload.attempt.state === "succeeded" ? "success" : ""
      );
    } catch (error) {
      setStatus(error.message || "Approved publish attempt could not be executed.", "error");
      try {
        const payload = await apiJson(`publishing/attempts/${encodeURIComponent(attemptId)}`);
        drawAttempt(payload);
      } catch (_) {}
    }
  });

  async function refresh() {
    const bearer = await token();
    setHidden(panel, !bearer);
    if (!bearer) {
      currentCandidate = null;
      loadedAttemptId = null;
      candidateView.replaceChildren();
      attemptView.replaceChildren();
      approveButton.disabled = true;
      executeButton.disabled = true;
      return;
    }
    try {
      await refreshProviderStatus();
    } catch (error) {
      setStatus(error.message || "Publishing status could not be loaded.", "error");
    }
  }

  const refreshButton = document.getElementById("refresh-button");
  if (refreshButton) refreshButton.addEventListener("click", refresh);
  window.addEventListener("focus", refresh);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  refresh();
})();
