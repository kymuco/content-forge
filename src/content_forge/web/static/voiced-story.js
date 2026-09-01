"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("voiced-story-panel");
  const projectInput = document.getElementById("voiced-story-project");
  const projectOptions = document.getElementById("voiced-story-project-options");
  const previewButton = document.getElementById("voiced-story-preview");
  const materializeButton = document.getElementById("voiced-story-materialize");
  const scenesNode = document.getElementById("voiced-story-scenes");
  const statusNode = document.getElementById("voiced-story-status");
  const countNode = document.getElementById("voiced-story-count");

  if (!panel || !projectInput || !projectOptions || !previewButton || !materializeButton
      || !scenesNode || !statusNode || !countNode) return;

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
  function seconds(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(3)}s` : "—";
  }
  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }
  async function api(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this device before reviewing voiced stories.");
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

  function drawManifest(manifest, mode) {
    scenesNode.replaceChildren();
    const scenes = Array.isArray(manifest && manifest.scenes) ? manifest.scenes : [];
    countNode.textContent = String(scenes.length);
    if (!scenes.length) {
      scenesNode.appendChild(text("div", "No timed voiced scenes are available.", "empty-state"));
      return;
    }
    for (const scene of scenes) {
      const card = document.createElement("article");
      card.className = "review-card";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.appendChild(text("strong", `Panel ${scene.scene_id}`));
      left.appendChild(text(
        "p",
        `${Array.isArray(scene.lines) ? scene.lines.length : 0} line(s) · ${seconds(scene.duration_seconds)}`,
        "muted compact-text"
      ));
      heading.appendChild(left);
      heading.appendChild(text("span", mode, mode === "materialized" ? "badge success" : "badge neutral"));
      card.appendChild(heading);

      for (const line of Array.isArray(scene.lines) ? scene.lines : []) {
        const block = document.createElement("div");
        block.className = "stack compact";
        block.appendChild(text(
          "strong",
          `${line.speaker_id} · ${line.cast_id}@${line.cast_revision}`
        ));
        block.appendChild(text("p", line.source_text, "compact-text"));
        block.appendChild(text(
          "p",
          `${seconds(line.start_seconds)} → ${seconds(line.end_seconds)} · audio ${seconds(line.audio_duration_seconds)}`,
          "muted compact-text mono wrap"
        ));
        const cues = Array.isArray(line.cues) ? line.cues : [];
        if (cues.length) {
          const cueList = document.createElement("div");
          cueList.className = "stack compact";
          for (const cue of cues) {
            cueList.appendChild(text(
              "p",
              `${seconds(cue.start_seconds)}–${seconds(cue.end_seconds)}  ${cue.text}`,
              "muted compact-text mono wrap"
            ));
          }
          block.appendChild(cueList);
        }
        card.appendChild(block);
      }
      scenesNode.appendChild(card);
    }
  }

  function selectedProjectId() {
    const projectId = projectInput.value.trim();
    if (!projectId) throw new Error("Choose or paste a project ID.");
    return projectId;
  }

  async function preview() {
    const projectId = selectedProjectId();
    setStatus(`Deriving voiced timing for ${projectId}…`);
    const manifest = await apiJson(
      `voiced-story/projects/${encodeURIComponent(projectId)}/preview`
    );
    drawManifest(manifest, "derived");
    setStatus("Derived timing is current but not written to Project state.", "success");
  }

  async function materialize() {
    const projectId = selectedProjectId();
    setStatus(`Materializing voiced timing for ${projectId}…`);
    const manifest = await apiJson(
      `voiced-story/projects/${encodeURIComponent(projectId)}/materialize`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }
    );
    drawManifest(manifest, "materialized");
    setStatus("Voiced-story timing is materialized from current PR19/PR20/PR21 authority.", "success");
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
        setStatus("Choose a project with accepted dialogue, cast bindings, and current synthesis.");
      }
    } catch (error) {
      setStatus(error.message || "Voiced Story could not be initialized.", "error");
    }
  }

  async function guardedAction(button, action) {
    button.disabled = true;
    try { await action(); }
    catch (error) { setStatus(error.message || "Voiced Story action failed.", "error"); }
    finally { button.disabled = false; }
  }

  previewButton.addEventListener("click", () => guardedAction(previewButton, preview));
  materializeButton.addEventListener("click", () => guardedAction(materializeButton, materialize));

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
