"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("dialogue-panel");
  const list = document.getElementById("dialogue-list");
  const count = document.getElementById("dialogue-count");
  const status = document.getElementById("dialogue-status");

  if (!panel || !list || !count || !status) return;

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
    if (!bearer) throw new Error("Pair this device before assigning dialogue.");
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
  function numberInput(labelText, initial, min, max) {
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.type = "number";
    input.min = String(min);
    input.max = String(max);
    input.step = "0.001";
    input.value = String(initial);
    label.appendChild(input);
    return { label, input };
  }
  function finiteUnit(input, label) {
    const value = Number(input.value);
    if (!Number.isFinite(value) || value < 0 || value > 1) {
      throw new Error(`${label} must be between 0 and 1.`);
    }
    return value;
  }

  function renderDialogueTask(item) {
    const task = item.task || {};
    const payload = task.payload || {};
    const rawRegions = Array.isArray(payload.regions) ? payload.regions : [];
    const characters = Array.isArray(payload.characters) ? payload.characters : [];
    const regionById = new Map(
      rawRegions
        .filter((region) => region && typeof region.region_id === "string")
        .map((region) => [region.region_id, region])
    );
    let order = rawRegions
      .map((region) => region && region.region_id)
      .filter((value) => typeof value === "string");
    const speakers = {};

    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.appendChild(text("strong", "Dialogue speaker assignment"));
    left.appendChild(text(
      "p",
      `${item.content_kind} · ${payload.scene_id || "unknown scene"}`,
      "muted compact-text mono wrap"
    ));
    heading.appendChild(left);
    heading.appendChild(text("span", task.priority || "high", "badge neutral"));
    card.appendChild(heading);
    card.appendChild(text(
      "p",
      "OCR order is only the starting evidence. Confirm the reading order and explicitly choose a speaker for every line before saving.",
      "muted compact-text"
    ));

    const suggestionBox = document.createElement("div");
    suggestionBox.className = "stack compact";
    const suggestions = Array.isArray(task.suggestions) ? task.suggestions : [];
    if (suggestions.length) {
      suggestionBox.appendChild(text("strong", "Assisted proposals (prefill only)"));
      for (const suggestion of suggestions) {
        suggestionBox.appendChild(button(
          `Use ${suggestion.label || "proposal"}`,
          "ghost",
          async () => {
            const value = suggestion && suggestion.value;
            if (!value || typeof value !== "object") throw new Error("Suggestion is malformed.");
            if (Array.isArray(value.reading_order)) order = value.reading_order.slice();
            if (value.speaker_by_region && typeof value.speaker_by_region === "object") {
              for (const regionId of regionById.keys()) {
                speakers[regionId] = typeof value.speaker_by_region[regionId] === "string"
                  ? value.speaker_by_region[regionId]
                  : "";
              }
            }
            setFocus(value.focus_hint || null);
            drawRows();
            setStatus("Proposal copied into the editor. Nothing has been accepted yet.");
          }
        ));
      }
      card.appendChild(suggestionBox);
    }

    const rows = document.createElement("div");
    rows.className = "review-order stack compact";
    function characterSelect(regionId) {
      const select = document.createElement("select");
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Choose speaker…";
      select.appendChild(placeholder);
      for (const character of characters) {
        if (!character || typeof character.character_id !== "string") continue;
        const option = document.createElement("option");
        option.value = character.character_id;
        option.textContent = character.display_name || character.character_id;
        select.appendChild(option);
      }
      select.value = speakers[regionId] || "";
      select.addEventListener("change", () => { speakers[regionId] = select.value; });
      return select;
    }
    function drawRows() {
      rows.replaceChildren();
      order.forEach((regionId, index) => {
        const region = regionById.get(regionId);
        const row = document.createElement("div");
        row.className = "review-order-row";
        const content = document.createElement("div");
        content.className = "stack compact";
        content.appendChild(text("strong", `${index + 1}. ${regionId}`, "mono wrap"));
        content.appendChild(text("p", region && region.text ? region.text : "", "compact-text"));
        if (region && region.bbox) {
          const bbox = region.bbox;
          content.appendChild(text(
            "p",
            `bbox ${bbox.x_min},${bbox.y_min} → ${bbox.x_max},${bbox.y_max}`,
            "muted compact-text mono"
          ));
        }
        content.appendChild(characterSelect(regionId));
        const actions = document.createElement("div");
        actions.className = "row";
        actions.appendChild(button("↑", "ghost", async () => {
          if (index === 0) return;
          [order[index - 1], order[index]] = [order[index], order[index - 1]];
          drawRows();
        }));
        actions.appendChild(button("↓", "ghost", async () => {
          if (index === order.length - 1) return;
          [order[index + 1], order[index]] = [order[index], order[index + 1]];
          drawRows();
        }));
        row.append(content, actions);
        rows.appendChild(row);
      });
    }
    drawRows();
    card.appendChild(rows);

    const focusField = document.createElement("label");
    focusField.textContent = "Scene focus hint";
    const focusMode = document.createElement("select");
    for (const [value, label] of [
      ["", "None"],
      ["speaker", "Speaker"],
      ["face", "Face point"],
      ["explicit_crop", "Explicit crop"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      focusMode.appendChild(option);
    }
    focusField.appendChild(focusMode);
    card.appendChild(focusField);

    const focusDetails = document.createElement("div");
    focusDetails.className = "row";
    const faceX = numberInput("Face X", 0.5, 0, 1);
    const faceY = numberInput("Face Y", 0.5, 0, 1);
    const cropX = numberInput("Crop X", 0, 0, 1);
    const cropY = numberInput("Crop Y", 0, 0, 1);
    const cropW = numberInput("Crop W", 1, 0, 1);
    const cropH = numberInput("Crop H", 1, 0, 1);

    function drawFocus() {
      focusDetails.replaceChildren();
      if (focusMode.value === "face") {
        focusDetails.append(faceX.label, faceY.label);
      } else if (focusMode.value === "explicit_crop") {
        focusDetails.append(cropX.label, cropY.label, cropW.label, cropH.label);
      }
    }
    function setFocus(value) {
      focusMode.value = value && typeof value.mode === "string" ? value.mode : "";
      if (value && value.face) {
        faceX.input.value = String(value.face.x);
        faceY.input.value = String(value.face.y);
      }
      if (value && value.crop) {
        cropX.input.value = String(value.crop.x);
        cropY.input.value = String(value.crop.y);
        cropW.input.value = String(value.crop.width);
        cropH.input.value = String(value.crop.height);
      }
      drawFocus();
    }
    focusMode.addEventListener("change", drawFocus);
    drawFocus();
    card.appendChild(focusDetails);

    function focusValue() {
      if (!focusMode.value) return null;
      if (focusMode.value === "speaker") return { mode: "speaker" };
      if (focusMode.value === "face") {
        return {
          mode: "face",
          face: {
            x: finiteUnit(faceX.input, "Face X"),
            y: finiteUnit(faceY.input, "Face Y"),
          },
        };
      }
      const crop = {
        x: finiteUnit(cropX.input, "Crop X"),
        y: finiteUnit(cropY.input, "Crop Y"),
        width: finiteUnit(cropW.input, "Crop width"),
        height: finiteUnit(cropH.input, "Crop height"),
      };
      if (crop.width <= 0 || crop.height <= 0) throw new Error("Crop width and height must be positive.");
      if (crop.x + crop.width > 1 || crop.y + crop.height > 1) {
        throw new Error("Crop must remain inside the normalized canvas.");
      }
      return { mode: "explicit_crop", crop };
    }

    card.appendChild(button("Accept reading order & speakers", "primary", async () => {
      if (!order.length || order.length !== regionById.size || new Set(order).size !== regionById.size) {
        throw new Error("Reading order must include every OCR region exactly once.");
      }
      const speakerByRegion = {};
      for (const regionId of order) {
        const speaker = speakers[regionId];
        if (!speaker) throw new Error(`Choose a speaker for ${regionId}.`);
        speakerByRegion[regionId] = speaker;
      }
      setStatus("Saving explicit dialogue assignment…");
      try {
        await apiJson(
          `dialogue/projects/${encodeURIComponent(item.project_id)}/tasks/${encodeURIComponent(task.review_task_id)}/assign`,
          jsonPost({
            assignment: {
              reading_order: order,
              speaker_by_region: speakerByRegion,
              focus_hint: focusValue(),
            },
          })
        );
        setStatus("Dialogue assignment accepted.", "success");
        await refreshDialogue();
      } catch (error) {
        setStatus(error.message || "Dialogue assignment failed.", "error");
      }
    }));
    return card;
  }

  async function refreshDialogue() {
    const bearer = await token();
    setHidden(panel, !bearer);
    if (!bearer) {
      list.replaceChildren();
      count.textContent = "0";
      return;
    }
    try {
      const payload = await apiJson("dialogue/review-queue?limit=100");
      const items = Array.isArray(payload.items) ? payload.items : [];
      count.textContent = String(items.length);
      list.replaceChildren(...items.map(renderDialogueTask));
      if (!items.length) {
        list.appendChild(text(
          "div",
          "No dialogue assignments need attention.",
          "empty-state"
        ));
      }
      if (status.dataset.kind !== "error") {
        setStatus(items.length ? `${items.length} dialogue assignment(s) need attention.` : "Dialogue queue is clear.");
      }
    } catch (error) {
      setStatus(error.message || "Dialogue queue could not be loaded.", "error");
    }
  }

  const refreshButton = document.getElementById("refresh-button");
  if (refreshButton) refreshButton.addEventListener("click", refreshDialogue);
  window.addEventListener("focus", refreshDialogue);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshDialogue();
  });
  refreshDialogue();
})();
