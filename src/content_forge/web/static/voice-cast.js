"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("voice-cast-panel");
  const registryList = document.getElementById("voice-cast-registry");
  const characterList = document.getElementById("voice-cast-characters");
  const status = document.getElementById("voice-cast-status");
  const count = document.getElementById("voice-cast-count");
  const projectInput = document.getElementById("voice-cast-project");
  const projectOptions = document.getElementById("voice-cast-project-options");
  const loadProjectButton = document.getElementById("voice-cast-load-project");
  const createForm = document.getElementById("voice-cast-create-form");

  if (!panel || !registryList || !characterList || !status || !count
      || !projectInput || !projectOptions || !loadProjectButton || !createForm) return;

  let casts = [];
  let loadedProject = null;
  let activeAudioUrl = null;
  let activeAudio = null;

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
      try {
        await onClick();
      } catch (error) {
        setStatus(error && error.message ? error.message : "Voice Cast action failed.", "error");
      } finally {
        item.disabled = false;
      }
    });
    return item;
  }
  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }
  async function api(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this device before managing Voice Cast.");
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
  function jsonRequest(method, body) {
    return {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }
  function nullableTrimmed(value) {
    const normalized = String(value || "").trim();
    return normalized || null;
  }

  function castLabel(item) {
    return `${item.display_name || item.cast_id} · ${item.cast_id}@${item.revision}`;
  }

  function drawRegistry() {
    count.textContent = String(casts.length);
    registryList.replaceChildren();
    if (!casts.length) {
      registryList.appendChild(text(
        "div",
        "No persistent voices yet. Create the first cast voice below.",
        "empty-state"
      ));
      return;
    }
    for (const item of casts) {
      const card = document.createElement("article");
      card.className = "review-card";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.appendChild(text("strong", item.display_name || item.cast_id));
      left.appendChild(text("p", `${item.cast_id}@${item.revision}`, "muted compact-text mono wrap"));
      heading.appendChild(left);
      heading.appendChild(text("span", item.settings && item.settings.voice_id ? item.settings.voice_id : "voice", "badge neutral"));
      card.appendChild(heading);
      const details = [];
      if (item.settings && item.settings.language) details.push(item.settings.language);
      if (item.settings && item.settings.instruction) details.push(`style: ${item.settings.instruction}`);
      if (item.settings && item.settings.reference_asset_id) details.push("reference audio");
      if (details.length) card.appendChild(text("p", details.join(" · "), "muted compact-text"));
      registryList.appendChild(card);
    }
  }

  function bindingFor(characterId) {
    const bindings = loadedProject && Array.isArray(loadedProject.bindings)
      ? loadedProject.bindings
      : [];
    return bindings.find((item) => item.character_id === characterId) || null;
  }

  function castById(castId) {
    return casts.find((item) => item.cast_id === castId) || null;
  }

  function stopPreview() {
    if (activeAudio) {
      activeAudio.pause();
      activeAudio.src = "";
      activeAudio = null;
    }
    if (activeAudioUrl) {
      URL.revokeObjectURL(activeAudioUrl);
      activeAudioUrl = null;
    }
  }

  async function playPreview(projectId, characterId) {
    setStatus(`Generating preview for ${characterId}…`);
    const response = await api(
      `voice-cast/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(characterId)}/preview`,
      jsonRequest("POST", {})
    );
    const blob = await response.blob();
    if (!blob.size) throw new Error("Voice preview returned an empty audio file.");
    stopPreview();
    activeAudioUrl = URL.createObjectURL(blob);
    activeAudio = new Audio(activeAudioUrl);
    await activeAudio.play();
    const resolved = response.headers.get("X-Content-Forge-Cast");
    setStatus(`Playing ${resolved || characterId}.`, "success");
  }

  function drawCharacters() {
    characterList.replaceChildren();
    if (!loadedProject) {
      characterList.appendChild(text(
        "div",
        "Choose a project with accepted dialogue to assign its characters.",
        "empty-state"
      ));
      return;
    }
    const characters = Array.isArray(loadedProject.characters) ? loadedProject.characters : [];
    if (!characters.length) {
      characterList.appendChild(text("div", "This project has no accepted PR19 characters.", "empty-state"));
      return;
    }
    for (const character of characters) {
      const binding = bindingFor(character.character_id);
      const card = document.createElement("article");
      card.className = "review-card";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.appendChild(text("strong", character.display_name || character.character_id));
      left.appendChild(text("p", character.character_id, "muted compact-text mono wrap"));
      heading.appendChild(left);
      heading.appendChild(text(
        "span",
        binding ? `${binding.cast_id}@${binding.cast_revision}` : "unassigned",
        binding ? "badge success" : "badge neutral"
      ));
      card.appendChild(heading);

      const castLabelNode = document.createElement("label");
      castLabelNode.textContent = "Persistent cast voice";
      const select = document.createElement("select");
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = casts.length ? "Choose cast…" : "Create a cast voice first";
      select.appendChild(placeholder);
      for (const item of casts) {
        const option = document.createElement("option");
        option.value = item.cast_id;
        option.textContent = castLabel(item);
        select.appendChild(option);
      }
      if (binding && castById(binding.cast_id)) select.value = binding.cast_id;
      castLabelNode.appendChild(select);
      card.appendChild(castLabelNode);

      const overrideLabel = document.createElement("label");
      overrideLabel.textContent = "Project-only voice ID override (optional)";
      const overrideInput = document.createElement("input");
      overrideInput.placeholder = "Leave blank to use the cast recipe";
      overrideInput.maxLength = 256;
      if (binding && binding.settings_override && binding.settings_override.voice_id) {
        overrideInput.value = binding.settings_override.voice_id;
      }
      overrideLabel.appendChild(overrideInput);
      card.appendChild(overrideLabel);

      const actions = document.createElement("div");
      actions.className = "row";
      actions.appendChild(button("Assign latest", "secondary", async () => {
        const castId = select.value;
        if (!castId) throw new Error("Choose a persistent cast voice first.");
        const selected = castById(castId);
        if (!selected || !selected.settings) throw new Error("Selected cast recipe is unavailable.");
        const overrideVoice = nullableTrimmed(overrideInput.value);
        const settingsOverride = overrideVoice
          ? Object.assign({}, selected.settings, { voice_id: overrideVoice })
          : null;
        loadedProject = await apiJson(
          `voice-cast/projects/${encodeURIComponent(loadedProject.project_id)}/characters/${encodeURIComponent(character.character_id)}`,
          jsonRequest("PUT", {
            cast_id: castId,
            settings_override: settingsOverride,
          })
        );
        stopPreview();
        drawCharacters();
        setStatus(`${character.display_name || character.character_id} assigned to ${castId}.`, "success");
      }));
      actions.appendChild(button("Preview voice", "primary", async () => {
        if (!bindingFor(character.character_id)) {
          throw new Error("Assign a cast voice before previewing it.");
        }
        await playPreview(loadedProject.project_id, character.character_id);
      }));
      if (binding) {
        actions.appendChild(button("Unassign", "ghost", async () => {
          loadedProject = await apiJson(
            `voice-cast/projects/${encodeURIComponent(loadedProject.project_id)}/characters/${encodeURIComponent(character.character_id)}`,
            { method: "DELETE" }
          );
          stopPreview();
          drawCharacters();
          setStatus(`${character.display_name || character.character_id} unassigned.`, "success");
        }));
      }
      card.appendChild(actions);
      characterList.appendChild(card);
    }
  }

  async function refreshRegistry() {
    const payload = await apiJson("voice-cast");
    casts = Array.isArray(payload.items) ? payload.items : [];
    drawRegistry();
    drawCharacters();
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

  async function loadProject() {
    const projectId = projectInput.value.trim();
    if (!projectId) throw new Error("Choose or paste a project ID.");
    stopPreview();
    loadedProject = await apiJson(`voice-cast/projects/${encodeURIComponent(projectId)}`);
    drawCharacters();
    setStatus(`Loaded ${projectId}.`, "success");
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const castId = document.getElementById("voice-cast-id").value.trim();
    const displayName = document.getElementById("voice-cast-name").value.trim();
    const voiceId = document.getElementById("voice-cast-voice-id").value.trim();
    const language = nullableTrimmed(document.getElementById("voice-cast-language").value);
    const instruction = nullableTrimmed(document.getElementById("voice-cast-instruction").value);
    if (!castId || !displayName || !voiceId) {
      setStatus("Cast ID, display name, and voice ID are required.", "error");
      return;
    }
    setStatus(`Saving ${castId}…`);
    try {
      await apiJson("voice-cast", jsonRequest("POST", {
        cast_id: castId,
        display_name: displayName,
        settings: {
          voice_id: voiceId,
          language,
          instruction,
        },
      }));
      await refreshRegistry();
      setStatus(`${castId} saved as an immutable cast revision.`, "success");
    } catch (error) {
      setStatus(error.message || "Voice Cast could not be saved.", "error");
    }
  });

  loadProjectButton.addEventListener("click", async () => {
    loadProjectButton.disabled = true;
    try { await loadProject(); }
    catch (error) { setStatus(error.message || "Project Voice Cast could not be loaded.", "error"); }
    finally { loadProjectButton.disabled = false; }
  });

  async function refresh() {
    const bearer = await token();
    setHidden(panel, !bearer);
    if (!bearer) {
      stopPreview();
      casts = [];
      loadedProject = null;
      registryList.replaceChildren();
      characterList.replaceChildren();
      count.textContent = "0";
      return;
    }
    try {
      await Promise.all([refreshRegistry(), refreshRecentProjectOptions()]);
      if (status.dataset.kind !== "error") {
        setStatus(`${casts.length} persistent cast voice(s) available.`);
      }
    } catch (error) {
      setStatus(error.message || "Voice Cast could not be loaded.", "error");
    }
  }

  const refreshButton = document.getElementById("refresh-button");
  if (refreshButton) refreshButton.addEventListener("click", refresh);
  window.addEventListener("focus", refresh);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  window.addEventListener("beforeunload", stopPreview);
  refresh();
})();