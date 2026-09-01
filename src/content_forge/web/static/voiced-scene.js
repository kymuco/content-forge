"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("voiced-scene-panel");
  const projectInput = document.getElementById("voiced-scene-project");
  const projectOptions = document.getElementById("voiced-scene-project-options");
  const previewButton = document.getElementById("voiced-scene-preview");
  const materializeButton = document.getElementById("voiced-scene-materialize");
  const dematerializeButton = document.getElementById("voiced-scene-dematerialize");
  const scenesNode = document.getElementById("voiced-scene-scenes");
  const statusNode = document.getElementById("voiced-scene-status");
  const countNode = document.getElementById("voiced-scene-count");

  if (!panel || !projectInput || !projectOptions || !previewButton || !materializeButton
      || !dematerializeButton || !scenesNode || !statusNode || !countNode) return;

  function setHidden(element, hidden) { element.classList.toggle("hidden", Boolean(hidden)); }
  function setStatus(message, kind) {
    statusNode.textContent = message || "";
    statusNode.dataset.kind = kind || "";
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
  async function api(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this device before reviewing scene presentation.");
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
    return response;
  }
  async function apiJson(relativePath, options) { return (await api(relativePath, options)).json(); }

  function selectedProjectId() {
    const projectId = projectInput.value.trim();
    if (!projectId) throw new Error("Choose or paste a project ID.");
    return projectId;
  }

  function issueBadge(issue) {
    const severity = issue && issue.severity === "blocking" ? "blocking" : "warning";
    return text(
      "span",
      `${severity}: ${issue && issue.code ? issue.code : "unknown_issue"}`,
      severity === "blocking" ? "badge danger" : "badge neutral"
    );
  }

  function drawPlan(payload, mode) {
    scenesNode.replaceChildren();
    const plan = payload && payload.plan ? payload.plan : payload;
    const scenes = Array.isArray(plan && plan.scenes) ? plan.scenes : [];
    const tracks = Array.isArray(plan && plan.tracks) ? plan.tracks : [];
    const issues = Array.isArray(plan && plan.issues) ? plan.issues : [];
    const blocking = issues.filter((issue) => issue && issue.severity === "blocking");
    countNode.textContent = String(scenes.length);

    const summary = document.createElement("article");
    summary.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.appendChild(text("strong", "Presentation plan"));
    const preset = plan && plan.preset ? plan.preset : {};
    left.appendChild(text(
      "p",
      `${preset.preset_id || "unknown"}@${preset.version || "?"} · ${tracks.length} duck target(s) · ${issues.length} QC issue(s)`,
      "muted compact-text"
    ));
    heading.appendChild(left);
    heading.appendChild(text(
      "span",
      blocking.length ? `${blocking.length} blocking` : mode,
      blocking.length ? "badge danger" : (mode === "materialized" ? "badge success" : "badge neutral")
    ));
    summary.appendChild(heading);
    if (issues.length) {
      const issueRow = document.createElement("div");
      issueRow.className = "row";
      for (const issue of issues) issueRow.appendChild(issueBadge(issue));
      summary.appendChild(issueRow);
    }
    scenesNode.appendChild(summary);

    if (!scenes.length) {
      scenesNode.appendChild(text("div", "No voiced scenes are available for presentation.", "empty-state"));
      return;
    }

    for (const scene of scenes) {
      const card = document.createElement("article");
      card.className = "review-card";
      const cardHeading = document.createElement("div");
      cardHeading.className = "card-heading";
      const cardLeft = document.createElement("div");
      cardLeft.appendChild(text("strong", `Panel ${scene.scene_id}`));
      cardLeft.appendChild(text(
        "p",
        `camera ${scene.camera_action || "retain"} · source ${scene.camera_source || "none"}`,
        "muted compact-text mono wrap"
      ));
      cardHeading.appendChild(cardLeft);
      cardHeading.appendChild(text(
        "span",
        scene.camera_action === "focus_zoom" ? "camera" : "retain",
        scene.camera_action === "focus_zoom" ? "badge success" : "badge neutral"
      ));
      card.appendChild(cardHeading);
      const sceneIssues = Array.isArray(scene.issues) ? scene.issues : [];
      if (sceneIssues.length) {
        const row = document.createElement("div");
        row.className = "row";
        for (const issue of sceneIssues) row.appendChild(issueBadge(issue));
        card.appendChild(row);
      }
      scenesNode.appendChild(card);
    }

    if (tracks.length) {
      const mix = document.createElement("article");
      mix.className = "review-card";
      mix.appendChild(text("strong", "Mix ducking"));
      for (const track of tracks) {
        mix.appendChild(text(
          "p",
          `${track.track_type} ${track.audio_track_id} · ${track.duck_db} dB during voiced intervals`,
          "muted compact-text mono wrap"
        ));
      }
      scenesNode.appendChild(mix);
    }
  }

  async function preview() {
    const projectId = selectedProjectId();
    setStatus(`Deriving presentation for ${projectId}…`);
    const plan = await apiJson(`voiced-scene/projects/${encodeURIComponent(projectId)}/preview`);
    drawPlan(plan, "derived");
    const blocking = Array.isArray(plan.issues)
      ? plan.issues.filter((issue) => issue && issue.severity === "blocking").length
      : 0;
    setStatus(
      blocking
        ? `Derived presentation has ${blocking} blocking QC issue(s); materialization will fail closed.`
        : "Derived camera and mix policy is current but not written to Project state.",
      blocking ? "error" : "success"
    );
  }

  async function materialize() {
    const projectId = selectedProjectId();
    setStatus(`Materializing presentation for ${projectId}…`);
    const manifest = await apiJson(
      `voiced-scene/projects/${encodeURIComponent(projectId)}/materialize`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }
    );
    drawPlan(manifest, "materialized");
    setStatus(
      "Camera and mix presentation are materialized over exact current PR22 authority.",
      "success"
    );
  }

  async function dematerialize() {
    const projectId = selectedProjectId();
    setStatus(`Removing PR23 presentation from ${projectId}…`);
    await api(
      `voiced-scene/projects/${encodeURIComponent(projectId)}/materialization`,
      { method: "DELETE" }
    );
    scenesNode.replaceChildren();
    countNode.textContent = "0";
    setStatus("PR23 presentation removed; PR22 timing and voice tracks were preserved.", "success");
  }

  async function refreshRecentProjectOptions() {
    const payload = await apiJson("inbox?limit=100");
    const ids = [];
    for (const item of Array.isArray(payload.items) ? payload.items : []) {
      if (item && typeof item.project_id === "string" && item.project_id && !ids.includes(item.project_id)) {
        ids.push(item.project_id);
      }
    }
    projectOptions.replaceChildren();
    for (const projectId of ids) {
      const option = document.createElement("option");
      option.value = projectId;
      projectOptions.appendChild(option);
    }
  }

  async function refresh() {
    const bearer = await token();
    setHidden(panel, !bearer);
    if (!bearer) {
      scenesNode.replaceChildren();
      countNode.textContent = "0";
      return;
    }
    try {
      await refreshRecentProjectOptions();
      if (!statusNode.dataset.kind) {
        setStatus("Choose a project with current materialized PR22 voiced-story state.");
      }
    } catch (error) {
      setStatus(error.message || "Scene Presentation could not be initialized.", "error");
    }
  }

  async function guardedAction(button, action) {
    button.disabled = true;
    try { await action(); }
    catch (error) { setStatus(error.message || "Scene Presentation action failed.", "error"); }
    finally { button.disabled = false; }
  }

  previewButton.addEventListener("click", () => guardedAction(previewButton, preview));
  materializeButton.addEventListener("click", () => guardedAction(materializeButton, materialize));
  dematerializeButton.addEventListener("click", () => guardedAction(dematerializeButton, dematerialize));

  const refreshButton = document.getElementById("refresh-button");
  if (refreshButton) refreshButton.addEventListener("click", refresh);
  const connectionBadge = document.getElementById("connection-badge");
  if (connectionBadge) {
    new MutationObserver(() => { refresh(); }).observe(connectionBadge, {
      childList: true,
      characterData: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["class"],
    });
  }
  window.addEventListener("focus", refresh);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  refresh();
})();
