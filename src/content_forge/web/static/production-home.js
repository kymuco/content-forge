"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("production-home-panel");
  const projectList = document.getElementById("production-home-projects");
  const summary = document.getElementById("production-home-summary");
  const count = document.getElementById("production-home-count");
  const status = document.getElementById("production-home-status");
  const addButton = document.getElementById("production-home-add");
  const advancedButton = document.getElementById("production-home-advanced");
  const connectionBadge = document.getElementById("connection-badge");
  const refreshButton = document.getElementById("refresh-button");
  const PROJECT_LIMIT = 48;
  const SOURCE_LIMIT = 48;
  const ADVANCED_PANEL_IDS = Object.freeze([
    "capture-panel",
    "review-panel",
    "dialogue-panel",
    "voice-cast-panel",
    "voiced-story-panel",
    "voiced-scene-panel",
    "production-profile-panel",
    "production-library-panel",
    "publishing-panel",
    "inbox-panel",
  ]);

  if (!panel || !projectList || !summary || !count || !status || !addButton || !advancedButton) return;
  summary.classList.remove("row");
  summary.classList.add("review-actions");

  let advancedVisible = false;
  let refreshGeneration = 0;
  let projectFlowGeneration = 0;
  let activeProjectId = null;
  let activeProjectLabel = null;
  let activeProjectState = null;
  const artifactUrls = new Set();

  let presets = [];
  let sources = [];
  let selectedPreset = null;
  let selectedSourceIds = [];
  let pendingCreateRequestId = null;
  const sourceThumbnailUrls = new Map();
  const sourceThumbnailPending = new Set();

  function setStatus(message, kind) {
    status.textContent = message || "";
    status.dataset.kind = kind || "";
  }

  function setHidden(element, hidden) {
    if (element) element.classList.toggle("hidden", Boolean(hidden));
  }

  function text(tag, value, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value == null ? "" : String(value);
    return node;
  }

  function button(label, className, onClick) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = className || "secondary";
    item.textContent = label;
    if (onClick) {
      item.addEventListener("click", async () => {
        item.disabled = true;
        try {
          await onClick();
        } catch (error) {
          setStatus(error && error.message ? error.message : "Production action failed.", "error");
        } finally {
          item.disabled = false;
        }
      });
    }
    return item;
  }

  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }

  async function api(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this phone before using Production.");
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

  async function apiJson(relativePath, options) {
    return (await api(relativePath, options)).json();
  }

  function jsonPost(body) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  function revokeArtifactUrls() {
    for (const url of artifactUrls) URL.revokeObjectURL(url);
    artifactUrls.clear();
  }

  function revokeSourceThumbnailUrls() {
    for (const url of sourceThumbnailUrls.values()) URL.revokeObjectURL(url);
    sourceThumbnailUrls.clear();
    sourceThumbnailPending.clear();
  }

  function applyAdvancedVisibility() {
    for (const id of ADVANCED_PANEL_IDS) {
      const element = document.getElementById(id);
      if (!element) continue;
      if (advancedVisible) element.classList.remove("hidden");
      else element.classList.add("hidden");
    }
    advancedButton.textContent = advancedVisible ? "Hide advanced" : "Advanced";
    advancedButton.setAttribute("aria-expanded", advancedVisible ? "true" : "false");
  }

  function watchAdvancedPanels() {
    const observer = new MutationObserver(() => {
      if (!advancedVisible) applyAdvancedVisibility();
    });
    for (const id of ADVANCED_PANEL_IDS) {
      const element = document.getElementById(id);
      if (element) observer.observe(element, { attributes: true, attributeFilter: ["class"] });
    }
  }

  function intakeLabel(intake, project) {
    if (intake && typeof intake.original_name === "string" && intake.original_name.trim()) {
      return intake.original_name.trim();
    }
    if (project && typeof project.production_preset_label === "string") {
      const sourceCount = Number(project.production_source_count || 0);
      return sourceCount > 1
        ? `${project.production_preset_label} · ${sourceCount} sources`
        : project.production_preset_label;
    }
    if (intake && typeof intake.note === "string" && intake.note.trim()) {
      return intake.note.trim().split(/\r?\n/, 1)[0].slice(0, 160);
    }
    if (intake && typeof intake.source_url === "string" && intake.source_url.trim()) {
      try { return new URL(intake.source_url).hostname || intake.source_url; }
      catch (_) { return intake.source_url.slice(0, 160); }
    }
    const kind = project && project.content_kind ? String(project.content_kind).replaceAll("_", " ") : "Video project";
    return kind.charAt(0).toUpperCase() + kind.slice(1);
  }

  function projectKindLabel(project) {
    if (project && typeof project.production_preset_label === "string") {
      return project.production_preset_label;
    }
    return String(project.content_kind || "project").replaceAll("_", " ");
  }

  function stateView(project) {
    const state = String(project.state || "").toUpperCase();
    const preview = project.preview && typeof project.preview === "object" ? project.preview : {};
    if (!project.review_initialized) {
      return { bucket: "inbox", badge: "Inbox", className: "badge neutral", detail: "Ready to start", action: "start" };
    }
    if (state === "NEEDS_REVIEW") {
      const open = Number(project.open_blocking_tasks || 0);
      const previewReady = preview.status === "ready";
      return {
        bucket: "attention",
        badge: previewReady ? "Preview ready" : "Needs attention",
        className: "badge state-partial",
        detail: previewReady ? "Watch and approve the preview" : `${open || 1} decision${open === 1 ? "" : "s"} before preview/final`,
        action: "continue",
      };
    }
    if (state === "READY") {
      return { bucket: "ready", badge: "Ready", className: "badge success", detail: "Approved for final render", action: "render" };
    }
    if (state === "RENDERING" || state === "QC") {
      return { bucket: "rendering", badge: "Rendering", className: "badge state-receiving", detail: "Desktop worker is finishing the video", action: "working" };
    }
    if (state === "DONE") {
      return { bucket: "ready", badge: "Finished", className: "badge success", detail: project.final ? "Final video is ready" : "Production completed", action: project.final ? "watch" : "done" };
    }
    return { bucket: "inbox", badge: state || "Project", className: "badge neutral", detail: "Continue production", action: "continue" };
  }

  function refreshExistingSurfaces() {
    if (refreshButton) refreshButton.click();
  }

  const projectFlowPanel = document.createElement("section");
  projectFlowPanel.id = "project-flow-panel";
  projectFlowPanel.className = "panel hidden";
  const projectFlowHeading = document.createElement("div");
  projectFlowHeading.className = "panel-heading";
  const projectFlowTitle = document.createElement("div");
  projectFlowTitle.append(text("p", "PROJECT", "eyebrow"), text("h2", "Video project"));
  const projectFlowBadge = text("span", "Project", "badge neutral");
  projectFlowHeading.append(projectFlowTitle, projectFlowBadge);
  const projectFlowActions = document.createElement("div");
  projectFlowActions.className = "row";
  const projectBackButton = document.createElement("button");
  projectBackButton.type = "button";
  projectBackButton.className = "ghost";
  projectBackButton.textContent = "← Back to projects";
  projectFlowActions.appendChild(projectBackButton);
  const projectFlowBody = document.createElement("div");
  projectFlowBody.id = "project-flow-body";
  projectFlowBody.className = "review-list";
  const projectFlowStatus = text("p", "", "status");
  projectFlowPanel.append(projectFlowHeading, projectFlowActions, projectFlowBody, projectFlowStatus);
  panel.insertAdjacentElement("afterend", projectFlowPanel);

  function setProjectFlowStatus(message, kind) {
    projectFlowStatus.textContent = message || "";
    projectFlowStatus.dataset.kind = kind || "";
  }

  function flowButton(label, className, onClick) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = className || "secondary";
    item.textContent = label;
    item.addEventListener("click", async () => {
      item.disabled = true;
      try {
        await onClick();
      } catch (error) {
        setProjectFlowStatus(error && error.message ? error.message : "Project action failed.", "error");
      } finally {
        item.disabled = false;
      }
    });
    return item;
  }

  function taskIsOpen(task) {
    return task && String(task.status || "").toLowerCase() === "open";
  }

  function projectIsTerminal() {
    return activeProjectState === "RENDERING" || activeProjectState === "QC" || activeProjectState === "DONE";
  }

  function taskIsEditable(task) {
    return taskIsOpen(task) && !projectIsTerminal();
  }

  function taskLabel(taskType) {
    switch (taskType) {
      case "hook": return "Hook";
      case "crop_confirmation": return "Crop";
      case "metadata": return "Video details";
      case "source_order": return "Source order";
      case "source_setup": return "Source setup";
      default: return String(taskType || "Decision").replaceAll("_", " ");
    }
  }

  function taskCard(task, subtitle) {
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.append(text("strong", taskLabel(task.task_type)), text("p", subtitle, "muted compact-text"));
    const open = taskIsOpen(task);
    const editable = taskIsEditable(task);
    heading.append(
      left,
      text(
        "span",
        editable ? "Needs you" : (open ? "Locked" : "Done"),
        editable ? "badge state-partial" : (open ? "badge neutral" : "badge success")
      )
    );
    card.appendChild(heading);
    return card;
  }

  async function resolveProjectTask(task, value) {
    if (!activeProjectId) return;
    await apiJson(
      `projects/${encodeURIComponent(activeProjectId)}/review/${encodeURIComponent(task.review_task_id)}/resolve`,
      jsonPost({ value })
    );
    await refreshProjectFlow();
  }

  function renderResolvedDecision(card, task) {
    if (task.task_type === "hook" && typeof task.accepted_value === "string") {
      card.appendChild(text("p", task.accepted_value, "compact-text"));
      return;
    }
    if (task.task_type === "metadata" && task.accepted_value && typeof task.accepted_value === "object") {
      const title = task.accepted_value.title;
      card.appendChild(text("p", title || "Optional details saved.", "muted compact-text"));
      return;
    }
    if (taskIsOpen(task) && projectIsTerminal()) {
      card.appendChild(text("p", "Production has already crossed the final-render boundary; this unresolved optional decision is retained as history and is no longer editable.", "muted compact-text"));
      return;
    }
    card.appendChild(text("p", "This decision is locked into the current project revision.", "muted compact-text"));
  }

  function renderHookDecision(task) {
    const card = taskCard(task, "The text shown with this format.");
    if (!taskIsEditable(task)) {
      renderResolvedDecision(card, task);
      return card;
    }
    const form = document.createElement("form");
    form.className = "stack compact";
    const label = document.createElement("label");
    label.textContent = "Hook";
    const input = document.createElement("textarea");
    input.rows = 3;
    input.maxLength = 4096;
    input.value = task.payload && task.payload.current || "";
    label.appendChild(input);
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "primary";
    save.textContent = "Save hook";
    form.append(label, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        setProjectFlowStatus("Saving hook…");
        await resolveProjectTask(task, input.value);
        setProjectFlowStatus("Hook saved.", "success");
      } catch (error) {
        setProjectFlowStatus(error && error.message ? error.message : "Hook update failed.", "error");
      } finally {
        save.disabled = false;
      }
    });
    card.appendChild(form);
    return card;
  }

  function cropNumber(labelText, value) {
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.max = "1";
    input.step = "0.01";
    input.inputMode = "decimal";
    input.value = String(value);
    label.appendChild(input);
    return { label, input };
  }

  function validatedCrop(inputs) {
    const x = Number(inputs.x.value);
    const y = Number(inputs.y.value);
    const width = Number(inputs.width.value);
    const height = Number(inputs.height.value);
    if (![x, y, width, height].every(Number.isFinite)) throw new Error("Crop values must be numbers.");
    if (x < 0 || y < 0 || width <= 0 || height <= 0 || x > 1 || y > 1 || width > 1 || height > 1) {
      throw new Error("Crop values must stay inside the normalized 0–1 frame.");
    }
    if (x + width > 1.000001 || y + height > 1.000001) {
      throw new Error("Crop rectangle must fit completely inside the frame.");
    }
    return { x, y, width, height };
  }

  function renderCropDecision(task) {
    const card = taskCard(task, "Keep full frame or set a bounded crop for each scene.");
    if (!taskIsEditable(task)) {
      renderResolvedDecision(card, task);
      return card;
    }
    const sceneIds = task.payload && Array.isArray(task.payload.scene_ids)
      ? task.payload.scene_ids.filter((value) => typeof value === "string")
      : [];
    const stored = task.payload && task.payload.crops && typeof task.payload.crops === "object"
      ? task.payload.crops
      : {};
    const editors = [];
    sceneIds.forEach((sceneId, index) => {
      const raw = Object.prototype.hasOwnProperty.call(stored, sceneId) ? stored[sceneId] : null;
      const scene = document.createElement("div");
      scene.className = "stack compact";
      scene.appendChild(text("strong", `Scene ${index + 1}`));
      const fullLabel = document.createElement("label");
      const full = document.createElement("input");
      full.type = "checkbox";
      full.checked = raw == null;
      fullLabel.append(full, document.createTextNode(" Use full frame"));
      const x = cropNumber("X", raw && raw.x != null ? raw.x : 0);
      const y = cropNumber("Y", raw && raw.y != null ? raw.y : 0);
      const width = cropNumber("Width", raw && raw.width != null ? raw.width : 1);
      const height = cropNumber("Height", raw && raw.height != null ? raw.height : 1);
      const fields = document.createElement("div");
      fields.className = "row";
      fields.append(x.label, y.label, width.label, height.label);
      function updateDisabled() {
        for (const input of [x.input, y.input, width.input, height.input]) input.disabled = full.checked;
      }
      full.addEventListener("change", updateDisabled);
      updateDisabled();
      scene.append(fullLabel, fields);
      card.appendChild(scene);
      editors.push({ sceneId, full, x: x.input, y: y.input, width: width.input, height: height.input });
    });
    card.appendChild(flowButton("Save crop", "secondary", async () => {
      const crops = {};
      for (const editor of editors) {
        crops[editor.sceneId] = editor.full.checked ? null : validatedCrop(editor);
      }
      setProjectFlowStatus("Saving crop…");
      await resolveProjectTask(task, { crops });
      setProjectFlowStatus("Crop saved. Preview will use this exact project revision.", "success");
    }));
    return card;
  }

  function renderMetadataDecision(task) {
    const card = taskCard(task, "Optional title, description and hashtags retained with the project.");
    if (!taskIsEditable(task)) {
      renderResolvedDecision(card, task);
      return card;
    }
    const payload = task.payload || {};
    const form = document.createElement("form");
    form.className = "stack compact";
    const titleLabel = document.createElement("label");
    titleLabel.textContent = "Title";
    const titleInput = document.createElement("input");
    titleInput.maxLength = 4096;
    titleInput.value = payload.title || "";
    titleLabel.appendChild(titleInput);
    const descriptionLabel = document.createElement("label");
    descriptionLabel.textContent = "Description";
    const descriptionInput = document.createElement("textarea");
    descriptionInput.rows = 3;
    descriptionInput.maxLength = 20000;
    descriptionInput.value = payload.description || "";
    descriptionLabel.appendChild(descriptionInput);
    const hashtagsLabel = document.createElement("label");
    hashtagsLabel.textContent = "Hashtags (comma separated)";
    const hashtagsInput = document.createElement("input");
    hashtagsInput.value = Array.isArray(payload.hashtags) ? payload.hashtags.join(", ") : "";
    hashtagsLabel.appendChild(hashtagsInput);
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "secondary";
    save.textContent = "Save details";
    form.append(titleLabel, descriptionLabel, hashtagsLabel, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        const hashtags = hashtagsInput.value.split(",").map((value) => value.trim()).filter(Boolean);
        setProjectFlowStatus("Saving video details…");
        await resolveProjectTask(task, {
          title: titleInput.value || null,
          description: descriptionInput.value || null,
          hashtags,
        });
        setProjectFlowStatus("Video details saved.", "success");
      } catch (error) {
        setProjectFlowStatus(error && error.message ? error.message : "Video details update failed.", "error");
      } finally {
        save.disabled = false;
      }
    });
    card.appendChild(form);
    return card;
  }

  function revealAdvancedReview() {
    activeProjectId = null;
    activeProjectLabel = null;
    activeProjectState = null;
    projectFlowPanel.classList.add("hidden");
    panel.classList.remove("hidden");
    advancedVisible = true;
    applyAdvancedVisibility();
    const review = document.getElementById("review-panel");
    if (review) review.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderReadOnlyDecision(task) {
    const card = taskCard(task, task.task_type === "source_order"
      ? "Order is retained by project authority; PR33 does not add a second reorder path."
      : "This decision is outside the bounded phone editor.");
    const reason = task.payload && task.payload.reason;
    if (reason) card.appendChild(text("p", reason, "error-text"));
    if (taskIsEditable(task)) {
      card.appendChild(flowButton("Open Advanced review", "secondary", async () => revealAdvancedReview()));
    } else if (taskIsOpen(task) && projectIsTerminal()) {
      renderResolvedDecision(card, task);
    }
    return card;
  }

  function renderDecisionTask(task) {
    switch (task.task_type) {
      case "hook": return renderHookDecision(task);
      case "crop_confirmation": return renderCropDecision(task);
      case "metadata": return renderMetadataDecision(task);
      default: return renderReadOnlyDecision(task);
    }
  }

  async function attachProjectArtifact(container, endpoint, loadingText) {
    container.replaceChildren(text("p", loadingText, "muted"));
    try {
      const response = await api(endpoint);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      artifactUrls.add(url);
      const video = document.createElement("video");
      video.className = "review-video";
      video.controls = true;
      video.playsInline = true;
      video.src = url;
      container.replaceChildren(video);
    } catch (error) {
      container.replaceChildren(text("p", error && error.message ? error.message : "Video could not be loaded.", "error-text"));
    }
  }

  function renderSourceOrder(project) {
    const sourceCount = Number(project.production_source_count || 0);
    if (!sourceCount) return null;
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.append(
      text("strong", "Sources"),
      text("p", "Order is locked from the explicit Create video selection.", "muted compact-text")
    );
    heading.append(left, text("span", `${sourceCount} source${sourceCount === 1 ? "" : "s"}`, "badge neutral"));
    card.appendChild(heading);
    const order = document.createElement("div");
    order.className = "review-actions";
    for (let index = 0; index < sourceCount; index += 1) {
      order.appendChild(text("span", `#${index + 1}`, "badge neutral"));
    }
    card.appendChild(order);
    return card;
  }

  function renderPreviewStage(project, tasks) {
    const card = document.createElement("article");
    card.className = "review-card";
    const previewTask = tasks.find((task) => task.task_type === "preview_approval") || null;
    const preview = project.preview && typeof project.preview === "object" ? project.preview : {};
    const state = String(project.state || "").toUpperCase();
    const approved = previewTask && !taskIsOpen(previewTask);
    const blockers = tasks.filter((task) => (
      task.task_type !== "preview_approval" && taskIsOpen(task) && Boolean(task.blocking)
    ));
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.append(
      text("strong", "Preview"),
      text("p", approved ? "Approved preview pinned to this project revision." : "Low-resolution check before final render.", "muted compact-text")
    );
    heading.append(left, text("span", approved ? "Approved" : (preview.status === "ready" ? "Ready" : "Preview"), approved ? "badge success" : "badge neutral"));
    card.appendChild(heading);

    if (preview.status === "ready" && typeof preview.job_id === "string") {
      const shell = document.createElement("div");
      shell.className = "review-preview-shell";
      card.appendChild(shell);
      void attachProjectArtifact(shell, `render-jobs/${encodeURIComponent(preview.job_id)}/artifact`, "Loading authenticated preview…");
      if (!approved && state === "NEEDS_REVIEW") {
        const actions = document.createElement("div");
        actions.className = "review-actions";
        actions.appendChild(flowButton("Approve preview", "primary", async () => {
          setProjectFlowStatus("Approving preview…");
          await apiJson(
            `projects/${encodeURIComponent(activeProjectId)}/preview/${encodeURIComponent(preview.job_id)}/approve`,
            { method: "POST" }
          );
          await refreshProjectFlow();
          setProjectFlowStatus("Preview approved. Final render is now allowed.", "success");
        }));
        actions.appendChild(flowButton("Reject & edit", "danger", async () => {
          setProjectFlowStatus("Reopening editable decisions…");
          await apiJson(
            `projects/${encodeURIComponent(activeProjectId)}/preview/${encodeURIComponent(preview.job_id)}/reject`,
            jsonPost({})
          );
          await refreshProjectFlow();
          setProjectFlowStatus("Preview rejected. Editable decisions are open again on this screen.", "success");
        }));
        card.appendChild(actions);
      }
      return card;
    }

    if (!previewTask) {
      card.appendChild(text("p", "This project has no phone preview approval task yet.", "muted compact-text"));
      return card;
    }
    if (preview.status === "rendering") {
      card.appendChild(text("p", "Desktop worker is rendering the preview. Refresh if this page was reopened after a restart.", "muted compact-text"));
      card.appendChild(flowButton("Refresh", "secondary", refreshProjectFlow));
      return card;
    }
    if (blockers.length) {
      card.appendChild(text("p", `${blockers.length} blocking decision${blockers.length === 1 ? "" : "s"} remain above.`, "muted compact-text"));
      return card;
    }
    if (taskIsOpen(previewTask)) {
      card.appendChild(flowButton("Generate preview", "primary", async () => {
        setProjectFlowStatus("Rendering preview on the desktop…");
        await apiJson(`projects/${encodeURIComponent(activeProjectId)}/preview`, { method: "POST" });
        await refreshProjectFlow();
        setProjectFlowStatus("Preview ready. Watch it here before approving.", "success");
      }));
    }
    return card;
  }

  function renderFinalStage(project) {
    const card = document.createElement("article");
    card.className = "review-card";
    const state = String(project.state || "").toUpperCase();
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.append(
      text("strong", "Final video"),
      text("p", state === "DONE" ? "Authenticated final output is ready." : "Desktop worker renders the approved project at final quality.", "muted compact-text")
    );
    heading.append(left, text("span", state === "DONE" ? "Ready" : state || "Final", state === "DONE" ? "badge success" : "badge neutral"));
    card.appendChild(heading);

    if (state === "READY") {
      card.appendChild(flowButton("Render final", "primary", async () => {
        setProjectFlowStatus("Rendering the final video on the desktop…");
        await apiJson(`projects/${encodeURIComponent(activeProjectId)}/final`, { method: "POST" });
        await refreshProjectFlow();
        setProjectFlowStatus("Final video is ready.", "success");
      }));
    } else if (state === "RENDERING" || state === "QC") {
      card.appendChild(text("p", "No phone action is needed while the desktop finishes render/QC.", "muted compact-text"));
      card.appendChild(flowButton("Refresh progress", "secondary", refreshProjectFlow));
    } else if (state === "DONE" && project.final && project.final.artifact_endpoint) {
      const shell = document.createElement("div");
      shell.className = "review-preview-shell";
      card.appendChild(shell);
      void attachProjectArtifact(shell, project.final.artifact_endpoint, "Loading authenticated final video…");
    } else {
      card.appendChild(text("p", "Approve the preview before final render.", "muted compact-text"));
    }
    return card;
  }

  function renderProjectFlow(project) {
    projectFlowBody.replaceChildren();
    const label = activeProjectLabel || project.production_preset_label || projectKindLabel(project);
    projectFlowTitle.replaceChildren(text("p", "PROJECT", "eyebrow"), text("h2", label));
    const state = String(project.state || "Project");
    activeProjectState = state.toUpperCase();
    projectFlowBadge.textContent = state.replaceAll("_", " ");
    projectFlowBadge.className = activeProjectState === "DONE" ? "badge success" : "badge neutral";

    const overview = document.createElement("article");
    overview.className = "review-card";
    const overviewHeading = document.createElement("div");
    overviewHeading.className = "card-heading";
    const overviewLeft = document.createElement("div");
    overviewLeft.append(
      text("strong", project.production_preset_label || projectKindLabel(project)),
      text("p", "One project context from decisions through preview and final.", "muted compact-text")
    );
    overviewHeading.append(overviewLeft, text("span", `${Number(project.open_blocking_tasks || 0)} blocking`, "badge neutral"));
    overview.appendChild(overviewHeading);
    projectFlowBody.appendChild(overview);

    const order = renderSourceOrder(project);
    if (order) projectFlowBody.appendChild(order);

    const tasks = Array.isArray(project.tasks) ? project.tasks : [];
    const decisions = tasks.filter((task) => task.task_type !== "preview_approval");
    for (const task of decisions) projectFlowBody.appendChild(renderDecisionTask(task));
    projectFlowBody.appendChild(renderPreviewStage(project, tasks));
    projectFlowBody.appendChild(renderFinalStage(project));
  }

  async function refreshProjectFlow() {
    if (!activeProjectId) return;
    const generation = ++projectFlowGeneration;
    const projectId = activeProjectId;
    setProjectFlowStatus("Loading this project…");
    try {
      revokeArtifactUrls();
      const project = await apiJson(`projects/${encodeURIComponent(projectId)}`);
      if (generation !== projectFlowGeneration || activeProjectId !== projectId) return;
      renderProjectFlow(project);
      setProjectFlowStatus("All actions on this screen reuse the existing project/review/render authority.");
    } catch (error) {
      if (generation !== projectFlowGeneration || activeProjectId !== projectId) return;
      projectFlowBody.replaceChildren(text("div", "This project could not be loaded.", "empty-state"));
      setProjectFlowStatus(error && error.message ? error.message : "Project could not be loaded.", "error");
    }
  }

  async function openProject(projectId, label) {
    if (typeof projectId !== "string" || !projectId) return;
    closeCreateWizard();
    advancedVisible = false;
    applyAdvancedVisibility();
    activeProjectId = projectId;
    activeProjectLabel = label || null;
    activeProjectState = null;
    setHidden(panel, true);
    setHidden(projectFlowPanel, false);
    projectFlowPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    await refreshProjectFlow();
  }

  async function startProject(projectId, label) {
    setStatus("Preparing this project for phone review…");
    await apiJson(`projects/${encodeURIComponent(projectId)}/review/bootstrap`, { method: "POST" });
    await refreshHome();
    await openProject(projectId, label);
    setProjectFlowStatus("Project prepared. Make the decisions on this screen.", "success");
  }

  function closeProjectFlow() {
    activeProjectId = null;
    activeProjectLabel = null;
    activeProjectState = null;
    projectFlowGeneration += 1;
    revokeArtifactUrls();
    projectFlowBody.replaceChildren();
    setProjectFlowStatus("");
    setHidden(projectFlowPanel, true);
    setHidden(panel, false);
    void refreshHome();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  projectBackButton.addEventListener("click", closeProjectFlow);

  function renderProject(entry) {
    const project = entry.project;
    const intake = entry.intake;
    const view = stateView(project);
    const card = document.createElement("article");
    card.className = "review-card";

    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.appendChild(text("strong", intakeLabel(intake, project)));
    left.appendChild(text("p", `${projectKindLabel(project)} · ${view.detail}`, "muted compact-text"));
    heading.append(left, text("span", view.badge, view.className));
    card.appendChild(heading);
    const label = intakeLabel(intake, project);

    if (view.action === "start") {
      card.appendChild(button("Start video", "primary", () => startProject(project.project_id, label)));
    } else if (view.action === "working") {
      card.appendChild(button("View progress", "secondary", () => openProject(project.project_id, label)));
    } else if (view.action === "watch" && project.final && project.final.artifact_endpoint) {
      card.appendChild(button("View final", "primary", () => openProject(project.project_id, label)));
    } else if (view.action === "done") {
      card.appendChild(button("Open project", "secondary", () => openProject(project.project_id, label)));
    } else {
      card.appendChild(button("Continue", "primary", () => openProject(project.project_id, label)));
    }
    return { card, bucket: view.bucket };
  }

  function renderSummary(buckets) {
    summary.replaceChildren();
    const values = [
      ["Inbox", buckets.inbox],
      ["Attention", buckets.attention],
      ["Rendering", buckets.rendering],
      ["Ready", buckets.ready],
    ];
    for (const [label, value] of values) {
      const chip = text("span", `${label} ${value}`, "badge neutral");
      summary.appendChild(chip);
    }
  }

  async function loadProjectEntries() {
    const [inboxPayload, queuePayload, productionPayload] = await Promise.all([
      apiJson("inbox?limit=100"),
      apiJson("review-queue?limit=100"),
      apiJson("production/projects?limit=100"),
    ]);
    const inbox = Array.isArray(inboxPayload.items) ? inboxPayload.items : [];
    const queue = Array.isArray(queuePayload.items) ? queuePayload.items : [];
    const ready = Array.isArray(queuePayload.ready_projects) ? queuePayload.ready_projects : [];
    const production = Array.isArray(productionPayload.items) ? productionPayload.items : [];

    const intakeByProject = new Map();
    for (const item of inbox) {
      if (typeof item.project_id === "string" && !intakeByProject.has(item.project_id)) {
        intakeByProject.set(item.project_id, item);
      }
    }

    const orderedIds = [];
    const seen = new Set();
    function addId(value) {
      if (typeof value !== "string" || seen.has(value) || orderedIds.length >= PROJECT_LIMIT) return;
      seen.add(value);
      orderedIds.push(value);
    }
    for (const item of queue) addId(item.project_id);
    for (const item of ready) addId(item.project_id);
    for (const item of production) addId(item.project_id);
    for (const item of inbox) addId(item.project_id);

    const results = await Promise.allSettled(
      orderedIds.map((projectId) => apiJson(`projects/${encodeURIComponent(projectId)}`))
    );
    const entries = [];
    results.forEach((result, index) => {
      if (result.status !== "fulfilled") return;
      entries.push({ project: result.value, intake: intakeByProject.get(orderedIds[index]) || null });
    });
    return entries;
  }

  async function refreshHome() {
    const generation = ++refreshGeneration;
    const bearer = await token();
    if (generation !== refreshGeneration) return;
    if (!bearer) {
      activeProjectId = null;
      activeProjectLabel = null;
      activeProjectState = null;
      advancedVisible = false;
      setHidden(projectFlowPanel, true);
    }
    setHidden(panel, !bearer || Boolean(activeProjectId));
    if (!bearer) {
      projectList.replaceChildren();
      summary.replaceChildren();
      count.textContent = "0";
      closeCreateWizard();
      applyAdvancedVisibility();
      return;
    }
    setStatus("Loading production state…");
    try {
      if (!activeProjectId) revokeArtifactUrls();
      const entries = await loadProjectEntries();
      if (generation !== refreshGeneration) return;
      const buckets = { inbox: 0, attention: 0, rendering: 0, ready: 0 };
      const rendered = entries.map(renderProject);
      for (const item of rendered) buckets[item.bucket] += 1;
      projectList.replaceChildren(...rendered.map((item) => item.card));
      count.textContent = String(entries.length);
      renderSummary(buckets);
      if (!entries.length) {
        projectList.appendChild(text("div", "Share a clip or image to Content Forge to start your first video.", "empty-state"));
        setStatus("Your production queue is empty.");
      } else {
        setStatus(buckets.attention ? `${buckets.attention} project(s) need a quick decision.` : "Production is up to date.");
      }
    } catch (error) {
      if (generation !== refreshGeneration) return;
      setStatus(error.message || "Production state could not be loaded.", "error");
    }
    applyAdvancedVisibility();
  }

  // PR32 create-video wizard. Build it from DOM primitives so the PWA shell keeps one
  // product surface and no new router/state authority is introduced.
  const homeActionRow = addButton.parentElement;
  const createVideoButton = document.createElement("button");
  createVideoButton.id = "production-home-create";
  createVideoButton.type = "button";
  createVideoButton.className = "primary";
  createVideoButton.textContent = "Create video";
  if (homeActionRow) {
    addButton.className = "secondary";
    homeActionRow.insertBefore(createVideoButton, addButton);
  }

  const createPanel = document.createElement("section");
  createPanel.id = "create-video-panel";
  createPanel.className = "panel hidden";
  const createHeading = document.createElement("div");
  createHeading.className = "panel-heading";
  const createHeadingText = document.createElement("div");
  createHeadingText.append(text("p", "CREATE VIDEO", "eyebrow"), text("h2", "Choose format & sources"));
  const createBadge = text("span", "Format", "badge neutral");
  createHeading.append(createHeadingText, createBadge);
  const createIntro = text(
    "p",
    "Pick a human-facing format, then select source media in the exact order you want it used. Content Forge pins the existing registered template and prepares the normal review/preview flow.",
    "muted"
  );
  const presetList = document.createElement("div");
  presetList.id = "create-video-presets";
  presetList.className = "review-list";
  const sourceHeading = text("strong", "Sources");
  const selectionStatus = text("p", "Choose a format first.", "muted compact-text");
  const sourceList = document.createElement("div");
  sourceList.id = "create-video-sources";
  sourceList.className = "review-list";
  const createActions = document.createElement("div");
  createActions.className = "row";
  const createProjectButton = document.createElement("button");
  createProjectButton.id = "create-video-submit";
  createProjectButton.type = "button";
  createProjectButton.className = "primary";
  createProjectButton.textContent = "Create project";
  createProjectButton.disabled = true;
  const cancelCreateButton = document.createElement("button");
  cancelCreateButton.type = "button";
  cancelCreateButton.className = "ghost";
  cancelCreateButton.textContent = "Cancel";
  createActions.append(createProjectButton, cancelCreateButton);
  const createStatus = text("p", "", "status");
  createPanel.append(
    createHeading,
    createIntro,
    presetList,
    sourceHeading,
    selectionStatus,
    sourceList,
    createActions,
    createStatus
  );
  projectFlowPanel.insertAdjacentElement("afterend", createPanel);

  function setCreateStatus(message, kind) {
    createStatus.textContent = message || "";
    createStatus.dataset.kind = kind || "";
  }

  function createRequestId() {
    if (crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }

  function invalidatePendingCreate() {
    pendingCreateRequestId = null;
  }

  function selectedSourceIndex(sourceProjectId) {
    return selectedSourceIds.indexOf(sourceProjectId);
  }

  function compatibleSources() {
    if (!selectedPreset) return [];
    return sources.filter((source) => !selectedPreset.image_only || source.media_type === "image");
  }

  function updateCreateValidity() {
    if (!selectedPreset) {
      selectionStatus.textContent = "Choose a format first.";
      createProjectButton.disabled = true;
      return;
    }
    const selected = selectedSourceIds.length;
    const min = Number(selectedPreset.min_sources || 1);
    const max = Number(selectedPreset.max_sources || 1);
    selectionStatus.textContent = selected
      ? `${selected} selected · tap order is scene order · ${min}–${max} allowed`
      : `Select ${min === max ? min : `${min}–${max}`} source${max === 1 ? "" : "s"}.`;
    createProjectButton.disabled = selected < min || selected > max;
  }

  function renderPresets() {
    presetList.replaceChildren();
    if (!presets.length) {
      presetList.appendChild(text("div", "No production presets are available.", "empty-state"));
      return;
    }
    for (const preset of presets) {
      const card = document.createElement("article");
      card.className = "review-card";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.append(text("strong", preset.label), text("p", preset.description, "muted compact-text"));
      const selected = selectedPreset && selectedPreset.preset_id === preset.preset_id;
      heading.append(left, text("span", selected ? "Selected" : (preset.image_only ? "Images" : "Media"), selected ? "badge success" : "badge neutral"));
      card.appendChild(heading);
      card.appendChild(button(selected ? "Selected" : "Choose", selected ? "ghost" : "secondary", async () => {
        if (selected) return;
        selectedPreset = preset;
        selectedSourceIds = selectedSourceIds
          .filter((sourceId) => {
            const source = sources.find((item) => item.source_project_id === sourceId);
            return source && (!preset.image_only || source.media_type === "image");
          })
          .slice(0, Number(preset.max_sources || 1));
        invalidatePendingCreate();
        renderPresets();
        renderSources();
        updateCreateValidity();
        createBadge.textContent = preset.label;
        setCreateStatus("");
      }));
      presetList.appendChild(card);
    }
  }

  async function loadSourceThumbnail(source, shell) {
    const sourceId = source.source_project_id;
    if (sourceThumbnailUrls.has(sourceId)) {
      const image = document.createElement("img");
      image.className = "thumbnail";
      image.alt = "";
      image.src = sourceThumbnailUrls.get(sourceId);
      shell.replaceChildren(image);
      return;
    }
    if (sourceThumbnailPending.has(sourceId)) return;
    sourceThumbnailPending.add(sourceId);
    try {
      const response = await api(source.thumbnail_endpoint);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      sourceThumbnailUrls.set(sourceId, url);
      const image = document.createElement("img");
      image.className = "thumbnail";
      image.alt = "";
      image.src = url;
      shell.replaceChildren(image);
    } catch (_) {
      shell.replaceChildren(text("span", String(source.media_type || "media").toUpperCase(), "badge neutral"));
    } finally {
      sourceThumbnailPending.delete(sourceId);
    }
  }

  function moveSelectedSource(sourceId, delta) {
    const index = selectedSourceIndex(sourceId);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= selectedSourceIds.length) return;
    const next = selectedSourceIds.slice();
    [next[index], next[target]] = [next[target], next[index]];
    selectedSourceIds = next;
    invalidatePendingCreate();
    renderSources();
    updateCreateValidity();
  }

  function toggleSource(sourceId) {
    const index = selectedSourceIndex(sourceId);
    if (index >= 0) {
      selectedSourceIds = selectedSourceIds.filter((value) => value !== sourceId);
    } else {
      const max = Number(selectedPreset && selectedPreset.max_sources || 1);
      if (selectedSourceIds.length >= max) {
        setCreateStatus(`This format accepts at most ${max} source(s).`, "error");
        return;
      }
      selectedSourceIds = [...selectedSourceIds, sourceId];
    }
    invalidatePendingCreate();
    renderSources();
    updateCreateValidity();
    setCreateStatus("");
  }

  function renderSources() {
    sourceList.replaceChildren();
    if (!selectedPreset) {
      sourceList.appendChild(text("div", "Choose a format to see compatible media.", "empty-state"));
      updateCreateValidity();
      return;
    }
    const compatible = compatibleSources();
    if (!compatible.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.append(text("strong", selectedPreset.image_only ? "No prepared images" : "No prepared media"));
      empty.append(text("span", "Add media to Inbox first, then return here."));
      empty.appendChild(button("Add media", "secondary", async () => {
        closeCreateWizard();
        addButton.click();
      }));
      sourceList.appendChild(empty);
      updateCreateValidity();
      return;
    }

    for (const source of compatible) {
      const card = document.createElement("article");
      card.className = "inbox-card";
      const media = document.createElement("div");
      media.className = "card-media";
      media.appendChild(text("span", String(source.media_type || "media").toUpperCase(), "badge neutral"));
      void loadSourceThumbnail(source, media);

      const body = document.createElement("div");
      body.className = "card-body";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const index = selectedSourceIndex(source.source_project_id);
      heading.append(
        text("strong", source.label || "Media source"),
        text("span", index >= 0 ? `#${index + 1}` : String(source.media_type || "media"), index >= 0 ? "badge success" : "badge neutral")
      );
      body.appendChild(heading);
      if (source.media_type === "video" && Number(source.duration_seconds) > 0) {
        body.appendChild(text("p", `${Number(source.duration_seconds).toFixed(1)} sec`, "muted compact-text"));
      }
      const actions = document.createElement("div");
      actions.className = "review-actions";
      actions.appendChild(button(index >= 0 ? "Remove" : "Select", index >= 0 ? "ghost" : "secondary", async () => toggleSource(source.source_project_id)));
      if (index >= 0) {
        const reorder = document.createElement("div");
        reorder.className = "row";
        const up = button("↑", "ghost", async () => moveSelectedSource(source.source_project_id, -1));
        const down = button("↓", "ghost", async () => moveSelectedSource(source.source_project_id, 1));
        up.disabled = index === 0;
        down.disabled = index === selectedSourceIds.length - 1;
        reorder.append(up, down);
        actions.appendChild(reorder);
      }
      body.appendChild(actions);
      card.append(media, body);
      sourceList.appendChild(card);
    }
    updateCreateValidity();
  }

  async function refreshCreateCatalog() {
    setCreateStatus("Loading formats and prepared media…");
    const [presetPayload, sourcePayload] = await Promise.all([
      apiJson("production/presets"),
      apiJson(`production/sources?limit=${SOURCE_LIMIT}`),
    ]);
    presets = Array.isArray(presetPayload.items) ? presetPayload.items : [];
    sources = Array.isArray(sourcePayload.items) ? sourcePayload.items : [];
    if (selectedPreset) {
      selectedPreset = presets.find((item) => item.preset_id === selectedPreset.preset_id) || null;
    }
    const available = new Set(sources.map((item) => item.source_project_id));
    selectedSourceIds = selectedSourceIds.filter((sourceId) => available.has(sourceId));
    if (selectedPreset && selectedPreset.image_only) {
      const imageIds = new Set(sources.filter((item) => item.media_type === "image").map((item) => item.source_project_id));
      selectedSourceIds = selectedSourceIds.filter((sourceId) => imageIds.has(sourceId));
    }
    renderPresets();
    renderSources();
    updateCreateValidity();
    setCreateStatus(sources.length ? "Choose a format and source order." : "Add media before creating a video.");
  }

  async function openCreateWizard() {
    if (activeProjectId) closeProjectFlow();
    createPanel.classList.remove("hidden");
    createPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      await refreshCreateCatalog();
    } catch (error) {
      setCreateStatus(error && error.message ? error.message : "Could not load production formats.", "error");
    }
  }

  function closeCreateWizard() {
    createPanel.classList.add("hidden");
    selectedPreset = null;
    selectedSourceIds = [];
    pendingCreateRequestId = null;
    presets = [];
    sources = [];
    revokeSourceThumbnailUrls();
    presetList.replaceChildren();
    sourceList.replaceChildren();
    createBadge.textContent = "Format";
    setCreateStatus("");
    updateCreateValidity();
  }

  async function createProductionProject() {
    if (!selectedPreset || createProjectButton.disabled) return;
    if (!pendingCreateRequestId) pendingCreateRequestId = createRequestId();
    createProjectButton.disabled = true;
    setCreateStatus("Creating the exact production project…");
    try {
      const result = await apiJson("production/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: pendingCreateRequestId,
          preset_id: selectedPreset.preset_id,
          source_project_ids: selectedSourceIds,
        }),
      });
      const label = result.production_preset_label || selectedPreset.label;
      pendingCreateRequestId = null;
      closeCreateWizard();
      await refreshHome();
      await openProject(result.project_id, label);
      setProjectFlowStatus(`${label} created. Continue here through preview and final.`, "success");
    } catch (error) {
      setCreateStatus(
        `${error && error.message ? error.message : "Could not create project."} Retry is safe with the same request identity.`,
        "error"
      );
      updateCreateValidity();
    }
  }

  createVideoButton.addEventListener("click", () => void openCreateWizard());
  cancelCreateButton.addEventListener("click", closeCreateWizard);
  createProjectButton.addEventListener("click", () => void createProductionProject());

  addButton.addEventListener("click", () => {
    const input = document.getElementById("file-input");
    if (input) input.click();
    else {
      const capture = document.getElementById("capture-panel");
      if (capture) capture.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });

  advancedButton.addEventListener("click", () => {
    advancedVisible = !advancedVisible;
    applyAdvancedVisibility();
    if (advancedVisible) setStatus("Advanced control surfaces are visible until you hide them again.");
  });

  async function refreshVisibleSurface() {
    if (activeProjectId) await refreshProjectFlow();
    else await refreshHome();
  }

  if (refreshButton) refreshButton.addEventListener("click", () => void refreshVisibleSurface());
  window.addEventListener("focus", () => void refreshVisibleSurface());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void refreshVisibleSurface();
  });
  window.addEventListener("beforeunload", () => {
    revokeArtifactUrls();
    revokeSourceThumbnailUrls();
  });

  if (connectionBadge) {
    const observer = new MutationObserver(() => void refreshVisibleSurface());
    observer.observe(connectionBadge, { childList: true, attributes: true, subtree: true });
  }

  watchAdvancedPanels();
  applyAdvancedVisibility();
  void refreshHome();
})();
