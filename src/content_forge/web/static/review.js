"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("review-panel");
  const list = document.getElementById("review-list");
  const count = document.getElementById("review-count");
  const status = document.getElementById("review-status");
  const prepareButton = document.getElementById("prepare-review-button");
  const readyList = document.getElementById("ready-projects");
  const previewUrls = new Set();

  if (!panel || !list || !count || !status || !prepareButton || !readyList) return;

  function setStatus(message, kind) {
    status.textContent = message || "";
    status.dataset.kind = kind || "";
  }
  function setHidden(element, hidden) { element.classList.toggle("hidden", Boolean(hidden)); }
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
    item.addEventListener("click", async () => {
      item.disabled = true;
      try { await onClick(); } finally { item.disabled = false; }
    });
    return item;
  }
  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }
  async function api(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this device before using Review.");
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
  function jsonPost(body) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }
  function revokePreviewUrls() {
    for (const url of previewUrls) URL.revokeObjectURL(url);
    previewUrls.clear();
  }

  async function prepareInbox() {
    setStatus("Preparing Inbox projects for review…");
    const inbox = await apiJson("inbox?limit=100");
    const ids = [...new Set(
      (inbox.items || [])
        .map((item) => item.project_id)
        .filter((value) => typeof value === "string" && value)
    )];
    if (!ids.length) {
      setStatus("No Inbox projects are ready to prepare.");
      return;
    }
    let prepared = 0;
    const failures = [];
    for (const projectId of ids) {
      try {
        await apiJson(`projects/${encodeURIComponent(projectId)}/review/bootstrap`, {
          method: "POST",
        });
        prepared += 1;
      } catch (error) {
        failures.push(error.message || String(error));
      }
    }
    if (failures.length) {
      setStatus(
        `${prepared} project(s) prepared; ${failures.length} need attention. ${failures[0]}`,
        "error"
      );
    } else {
      setStatus(`${prepared} project(s) prepared for review.`, "success");
    }
    await refreshReview();
  }

  async function resolve(projectId, taskId, value) {
    await apiJson(
      `projects/${encodeURIComponent(projectId)}/review/${encodeURIComponent(taskId)}/resolve`,
      jsonPost({ value })
    );
    await refreshReview();
  }

  function taskHeading(card, item) {
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.appendChild(text("strong", item.task.task_type.replaceAll("_", " ")));
    left.appendChild(text(
      "p",
      `${item.content_kind} · ${item.project_state} · ${item.task.attention}`,
      "muted compact-text"
    ));
    heading.appendChild(left);
    heading.appendChild(text("span", item.task.priority, "badge neutral"));
    card.appendChild(heading);
  }

  function renderHook(card, item) {
    const form = document.createElement("form");
    form.className = "stack compact";
    const label = document.createElement("label");
    label.textContent = "Hook";
    const input = document.createElement("textarea");
    input.rows = 3;
    input.maxLength = 4096;
    input.value = item.task.payload.current || "";
    label.appendChild(input);
    form.appendChild(label);
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "primary";
    save.textContent = "Save hook";
    form.appendChild(save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        setStatus("Saving hook…");
        await resolve(item.project_id, item.task.review_task_id, input.value);
        setStatus("Hook accepted. Preview state invalidated until rerender.", "success");
      } catch (error) {
        setStatus(error.message || "Hook update failed.", "error");
      } finally { save.disabled = false; }
    });
    card.appendChild(form);
  }

  function renderCrop(card, item) {
    card.appendChild(text(
      "p",
      "Confirm the current canonical crop for every scene. Fine crop editing can remain a later/manual decision.",
      "muted compact-text"
    ));
    const sceneIds = Array.isArray(item.task.payload.scene_ids)
      ? item.task.payload.scene_ids.filter((value) => typeof value === "string")
      : [];
    const storedCrops = item.task.payload && item.task.payload.crops;
    const crops = {};
    for (const sceneId of sceneIds) {
      const raw = storedCrops && typeof storedCrops === "object"
        && Object.prototype.hasOwnProperty.call(storedCrops, sceneId)
        ? storedCrops[sceneId]
        : null;
      if (raw == null) {
        crops[sceneId] = null;
      } else if (typeof raw === "object" && !Array.isArray(raw)) {
        crops[sceneId] = {
          x: raw.x,
          y: raw.y,
          width: raw.width,
          height: raw.height,
        };
      } else {
        crops[sceneId] = null;
      }
    }
    card.appendChild(button("Confirm current crop", "secondary", async () => {
      setStatus("Saving crop confirmation…");
      try {
        await resolve(item.project_id, item.task.review_task_id, { crops });
        setStatus("Crop confirmed.", "success");
      } catch (error) {
        setStatus(error.message || "Crop confirmation failed.", "error");
      }
    }));
  }

  function renderOrder(card, item) {
    let order = Array.isArray(item.task.payload.scene_ids)
      ? item.task.payload.scene_ids.filter((value) => typeof value === "string")
      : [];
    const box = document.createElement("div");
    box.className = "review-order";
    function draw() {
      box.replaceChildren();
      order.forEach((sceneId, index) => {
        const row = document.createElement("div");
        row.className = "review-order-row";
        row.appendChild(text("span", `${index + 1}. ${sceneId}`, "mono wrap"));
        const actions = document.createElement("div");
        actions.className = "row";
        actions.appendChild(button("↑", "ghost", async () => {
          if (index === 0) return;
          [order[index - 1], order[index]] = [order[index], order[index - 1]];
          draw();
        }));
        actions.appendChild(button("↓", "ghost", async () => {
          if (index === order.length - 1) return;
          [order[index + 1], order[index]] = [order[index], order[index + 1]];
          draw();
        }));
        row.appendChild(actions);
        box.appendChild(row);
      });
    }
    draw();
    card.appendChild(box);
    card.appendChild(button("Save order", "secondary", async () => {
      setStatus("Saving scene order…");
      try {
        await resolve(item.project_id, item.task.review_task_id, order);
        setStatus("Order accepted.", "success");
      } catch (error) {
        setStatus(error.message || "Order update failed.", "error");
      }
    }));
  }

  function renderMetadata(card, item) {
    const form = document.createElement("form");
    form.className = "stack compact";
    const titleLabel = document.createElement("label");
    titleLabel.textContent = "Title";
    const titleInput = document.createElement("input");
    titleInput.maxLength = 4096;
    titleInput.value = item.task.payload.title || "";
    titleLabel.appendChild(titleInput);
    const descriptionLabel = document.createElement("label");
    descriptionLabel.textContent = "Description";
    const descriptionInput = document.createElement("textarea");
    descriptionInput.rows = 3;
    descriptionInput.maxLength = 20000;
    descriptionInput.value = item.task.payload.description || "";
    descriptionLabel.appendChild(descriptionInput);
    const hashtagsLabel = document.createElement("label");
    hashtagsLabel.textContent = "Hashtags (comma separated)";
    const hashtagsInput = document.createElement("input");
    hashtagsInput.value = Array.isArray(item.task.payload.hashtags)
      ? item.task.payload.hashtags.join(", ")
      : "";
    hashtagsLabel.appendChild(hashtagsInput);
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "secondary";
    save.textContent = "Save metadata";
    form.append(titleLabel, descriptionLabel, hashtagsLabel, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const hashtags = hashtagsInput.value
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
      save.disabled = true;
      try {
        setStatus("Saving metadata…");
        await resolve(item.project_id, item.task.review_task_id, {
          title: titleInput.value || null,
          description: descriptionInput.value || null,
          hashtags,
        });
        setStatus("Metadata accepted.", "success");
      } catch (error) {
        setStatus(error.message || "Metadata update failed.", "error");
      } finally { save.disabled = false; }
    });
    card.appendChild(form);
  }

  async function attachPreviewVideo(card, endpoint) {
    const shell = document.createElement("div");
    shell.className = "review-preview-shell";
    shell.appendChild(text("p", "Loading authenticated preview…", "muted"));
    card.appendChild(shell);
    try {
      const response = await api(endpoint);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      previewUrls.add(url);
      const video = document.createElement("video");
      video.className = "review-video";
      video.controls = true;
      video.playsInline = true;
      video.src = url;
      shell.replaceChildren(video);
    } catch (error) {
      shell.replaceChildren(text(
        "p",
        error.message || "Preview could not be loaded.",
        "error-text"
      ));
    }
  }

  function renderPreviewApproval(card, item) {
    const preview = item.task.payload || {};
    if (preview.status === "ready" && typeof preview.job_id === "string") {
      attachPreviewVideo(
        card,
        `render-jobs/${encodeURIComponent(preview.job_id)}/artifact`
      );
      const actions = document.createElement("div");
      actions.className = "review-actions";
      actions.appendChild(button("Approve preview", "primary", async () => {
        setStatus("Approving preview…");
        try {
          await apiJson(
            `projects/${encodeURIComponent(item.project_id)}/preview/${encodeURIComponent(preview.job_id)}/approve`,
            { method: "POST" }
          );
          setStatus("Preview approved. Project is ready for final render.", "success");
          await refreshReview();
        } catch (error) {
          setStatus(error.message || "Preview approval failed.", "error");
        }
      }));
      actions.appendChild(button("Reject & edit", "danger", async () => {
        setStatus("Reopening review decisions…");
        try {
          await apiJson(
            `projects/${encodeURIComponent(item.project_id)}/preview/${encodeURIComponent(preview.job_id)}/reject`,
            jsonPost({})
          );
          setStatus("Preview rejected. Editable decisions were reopened.", "success");
          await refreshReview();
        } catch (error) {
          setStatus(error.message || "Preview rejection failed.", "error");
        }
      }));
      card.appendChild(actions);
      return;
    }
    if (preview.status === "rejected") {
      card.appendChild(text(
        "p",
        "Preview was rejected. Resolve the reopened decisions, then render again.",
        "muted compact-text"
      ));
      return;
    }
    card.appendChild(button("Render 540×960 preview", "primary", async () => {
      setStatus("Rendering preview on the desktop…");
      try {
        await apiJson(`projects/${encodeURIComponent(item.project_id)}/preview`, {
          method: "POST",
        });
        setStatus("Preview render completed.", "success");
        await refreshReview();
      } catch (error) {
        setStatus(error.message || "Preview render failed.", "error");
      }
    }));
  }

  function renderManual(card, item) {
    const reason = item.task.payload && item.task.payload.reason;
    card.appendChild(text(
      "p",
      reason || "This decision requires a manual desktop step.",
      "error-text"
    ));
  }

  function renderTask(item) {
    const card = document.createElement("article");
    card.className = "review-card";
    taskHeading(card, item);
    switch (item.task.task_type) {
      case "hook": renderHook(card, item); break;
      case "crop_confirmation": renderCrop(card, item); break;
      case "source_order": renderOrder(card, item); break;
      case "metadata": renderMetadata(card, item); break;
      case "preview_approval": renderPreviewApproval(card, item); break;
      default: renderManual(card, item); break;
    }
    return card;
  }

  function renderReadyProject(project) {
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.appendChild(text("strong", "Ready for final render"));
    left.appendChild(text("p", project.project_id, "mono wrap compact-text"));
    heading.appendChild(left);
    heading.appendChild(text("span", project.state, "badge success"));
    card.appendChild(heading);
    card.appendChild(button("Render final", "primary", async () => {
      setStatus("Rendering final artifact on the desktop…");
      try {
        const result = await apiJson(
          `projects/${encodeURIComponent(project.project_id)}/final`,
          { method: "POST" }
        );
        setStatus(
          `Final render complete: ${result.width}×${result.height}, job ${result.job_id}.`,
          "success"
        );
        await refreshReview();
      } catch (error) {
        setStatus(error.message || "Final render failed.", "error");
      }
    }));
    return card;
  }

  async function refreshReview() {
    revokePreviewUrls();
    const bearer = await token();
    setHidden(panel, !bearer);
    if (!bearer) {
      list.replaceChildren();
      readyList.replaceChildren();
      count.textContent = "0";
      return;
    }
    try {
      const payload = await apiJson("review-queue?limit=100");
      const items = Array.isArray(payload.items) ? payload.items : [];
      const ready = Array.isArray(payload.ready_projects) ? payload.ready_projects : [];
      count.textContent = String(items.length);
      list.replaceChildren(...items.map(renderTask));
      readyList.replaceChildren(...ready.map(renderReadyProject));
      if (!items.length && !ready.length) {
        list.appendChild(text(
          "div",
          "No review tasks yet. Prepare Inbox projects to begin.",
          "empty-state"
        ));
      }
      if (!status.dataset.kind || status.dataset.kind !== "error") {
        setStatus(
          items.length
            ? `${items.length} review task(s) need attention.`
            : "Review queue is clear."
        );
      }
    } catch (error) {
      setStatus(error.message || "Review queue could not be loaded.", "error");
    }
  }

  prepareButton.addEventListener("click", async () => {
    prepareButton.disabled = true;
    try { await prepareInbox(); }
    catch (error) { setStatus(error.message || "Review preparation failed.", "error"); }
    finally { prepareButton.disabled = false; }
  });
  const refreshButton = document.getElementById("refresh-button");
  if (refreshButton) refreshButton.addEventListener("click", refreshReview);
  window.addEventListener("focus", refreshReview);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshReview();
  });
  window.addEventListener("beforeunload", revokePreviewUrls);
  refreshReview();
})();
