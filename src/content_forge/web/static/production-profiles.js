"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("production-profile-panel");
  const registryList = document.getElementById("production-profile-registry");
  const count = document.getElementById("production-profile-count");
  const status = document.getElementById("production-profile-status");
  const createForm = document.getElementById("production-profile-create-form");
  const projectInput = document.getElementById("production-profile-project");
  const projectOptions = document.getElementById("production-profile-project-options");
  const profileSelect = document.getElementById("production-profile-select");
  const loadProjectButton = document.getElementById("production-profile-load-project");
  const bindButton = document.getElementById("production-profile-bind");
  const unbindButton = document.getElementById("production-profile-unbind");
  const projectView = document.getElementById("production-profile-project-view");

  if (!panel || !registryList || !count || !status || !createForm || !projectInput
      || !projectOptions || !profileSelect || !loadProjectButton || !bindButton
      || !unbindButton || !projectView) return;

  let profiles = [];
  let loadedProject = null;

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
  function currentProjectId() { return projectInput.value.trim(); }
  function updateUnbindAvailability() {
    const projectId = currentProjectId();
    if (!projectId) {
      unbindButton.disabled = true;
      return;
    }
    if (loadedProject && loadedProject.project_id === projectId) {
      unbindButton.disabled = !loadedProject.profile;
      return;
    }
    // An unreadable old profile may have a missing optional dependency. Keep the
    // explicit recovery action reachable by Project ID even when strict GET validation
    // cannot load that stale authority snapshot.
    unbindButton.disabled = false;
  }
  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }
  async function apiJson(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this device before managing production profiles.");
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
  function jsonRequest(method, body) {
    return {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }
  function selectedRevision() {
    const value = profileSelect.value;
    if (value) {
      const separator = value.lastIndexOf("@");
      if (separator >= 1) {
        const revision = Number(value.slice(separator + 1));
        if (Number.isInteger(revision) && revision >= 1) {
          return { profile_id: value.slice(0, separator), revision };
        }
      }
    }

    // Registry listing is intentionally fail-closed when any latest revision has stale
    // external evidence. Recovery rebind must still be possible, so allow the operator
    // to enter an exact known-good revision explicitly instead of depending on the list.
    const manualProfileId = window.prompt(
      "No selectable profile revision is available. Enter an exact profile ID for recovery rebind:"
    );
    if (manualProfileId == null || !manualProfileId.trim()) return null;
    const manualRevision = window.prompt("Enter the exact profile revision number:");
    if (manualRevision == null) return null;
    const revision = Number(manualRevision.trim());
    if (!Number.isInteger(revision) || revision < 1) return null;
    return { profile_id: manualProfileId.trim(), revision };
  }
  function profileLabel(item) {
    const definition = item.definition || {};
    return `${definition.display_name || item.profile_id} · ${item.profile_id}@${item.revision}`;
  }

  function drawRegistry() {
    count.textContent = String(profiles.length);
    registryList.replaceChildren();
    profileSelect.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = profiles.length ? "Choose production profile…" : "No selectable profiles — manual recovery remains available";
    profileSelect.appendChild(placeholder);

    if (!profiles.length) {
      registryList.appendChild(text(
        "div",
        "No validated production profiles are selectable. Create a profile, or use Bind / rebind and enter an exact profile ID + revision for recovery.",
        "empty-state"
      ));
      return;
    }

    for (const item of profiles) {
      const definition = item.definition || {};
      const option = document.createElement("option");
      option.value = `${item.profile_id}@${item.revision}`;
      option.textContent = profileLabel(item);
      profileSelect.appendChild(option);

      const card = document.createElement("article");
      card.className = "review-card";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.appendChild(text("strong", definition.display_name || item.profile_id));
      left.appendChild(text("p", `${item.profile_id}@${item.revision}`, "muted compact-text mono wrap"));
      heading.appendChild(left);
      heading.appendChild(text("span", definition.scope || "profile", "badge neutral"));
      card.appendChild(heading);

      const details = [];
      if (definition.default_template) {
        details.push(`template ${definition.default_template.template_id}@${definition.default_template.version}`);
      }
      const outputs = Array.isArray(definition.output_profiles) ? definition.output_profiles : [];
      if (outputs.length) details.push(`outputs ${outputs.map((entry) => entry.profile_id).join(", ")}`);
      const languages = Array.isArray(definition.default_languages) ? definition.default_languages : [];
      if (languages.length) details.push(`languages ${languages.join(", ")}`);
      const casts = Array.isArray(definition.cast_defaults) ? definition.cast_defaults : [];
      if (casts.length) details.push(`cast roles ${casts.length}`);
      const music = Array.isArray(definition.music_library) ? definition.music_library : [];
      const reactions = Array.isArray(definition.reaction_library) ? definition.reaction_library : [];
      if (music.length || reactions.length) details.push(`libraries ${music.length} music / ${reactions.length} reaction`);
      if (details.length) card.appendChild(text("p", details.join(" · "), "muted compact-text"));
      registryList.appendChild(card);
    }
  }

  function drawProject() {
    projectView.replaceChildren();
    if (!loadedProject) {
      projectView.appendChild(text(
        "div",
        "Choose a project to inspect or change its explicit production-profile snapshot.",
        "empty-state"
      ));
      updateUnbindAvailability();
      return;
    }
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.appendChild(text("strong", loadedProject.project_id));
    left.appendChild(text("p", loadedProject.project_state, "muted compact-text"));
    heading.appendChild(left);
    const manifest = loadedProject.profile;
    heading.appendChild(text(
      "span",
      manifest ? `${manifest.revision.profile_id}@${manifest.revision.revision}` : "unbound",
      manifest ? "badge success" : "badge neutral"
    ));
    card.appendChild(heading);
    if (loadedProject.template) {
      card.appendChild(text(
        "p",
        `Template: ${loadedProject.template.template_id}@${loadedProject.template.version}`,
        "muted compact-text"
      ));
    }
    const outputs = Array.isArray(loadedProject.output_profiles) ? loadedProject.output_profiles : [];
    if (outputs.length) {
      card.appendChild(text(
        "p",
        `Outputs: ${outputs.map((entry) => entry.profile_id).join(", ")}`,
        "muted compact-text"
      ));
    }
    if (manifest) {
      card.appendChild(text(
        "p",
        `Owned defaults: template ${manifest.applied_default_template ? "yes" : "no"} · outputs ${manifest.applied_output_profiles ? "yes" : "no"}`,
        "muted compact-text"
      ));
      profileSelect.value = `${manifest.revision.profile_id}@${manifest.revision.revision}`;
    }
    projectView.appendChild(card);
    updateUnbindAvailability();
  }

  async function refreshRegistry() {
    const payload = await apiJson("production-profiles");
    profiles = Array.isArray(payload.items) ? payload.items : [];
    drawRegistry();
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
    const projectId = currentProjectId();
    if (!projectId) throw new Error("Choose or paste a project ID.");
    loadedProject = null;
    drawProject();
    loadedProject = await apiJson(`production-profiles/projects/${encodeURIComponent(projectId)}`);
    drawProject();
    setStatus(`Loaded ${projectId}.`, "success");
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const profileId = document.getElementById("production-profile-id").value.trim();
    const displayName = document.getElementById("production-profile-name").value.trim();
    const scope = document.getElementById("production-profile-scope").value;
    const templateChoice = document.getElementById("production-profile-template").value;
    const languages = document.getElementById("production-profile-languages").value
      .split(",").map((item) => item.trim()).filter(Boolean);
    if (!profileId || !displayName) {
      setStatus("Profile ID and display name are required.", "error");
      return;
    }
    let defaultTemplate = null;
    if (templateChoice) {
      const [templateId, version] = templateChoice.split("@");
      defaultTemplate = { template_id: templateId, version };
    }
    setStatus(`Saving ${profileId}…`);
    try {
      await apiJson("production-profiles", jsonRequest("POST", {
        profile_id: profileId,
        scope,
        display_name: displayName,
        default_template: defaultTemplate,
        default_languages: languages,
        branding: { display_name: displayName },
      }));
      await refreshRegistry();
      setStatus(`${profileId} saved as an immutable revision.`, "success");
    } catch (error) {
      setStatus(error.message || "Production profile could not be saved.", "error");
    }
  });

  loadProjectButton.addEventListener("click", async () => {
    loadProjectButton.disabled = true;
    try { await loadProject(); }
    catch (error) {
      updateUnbindAvailability();
      setStatus(`${error.message || "Project profile could not be loaded."} You can still unbind or rebind by Project ID if an old optional profile dependency disappeared.`, "error");
    }
    finally { loadProjectButton.disabled = false; }
  });

  bindButton.addEventListener("click", async () => {
    bindButton.disabled = true;
    try {
      const projectId = currentProjectId();
      const selected = selectedRevision();
      if (!projectId) throw new Error("Choose or paste a project ID.");
      if (!selected) throw new Error("Choose or enter an exact production profile revision.");
      loadedProject = await apiJson(
        `production-profiles/projects/${encodeURIComponent(projectId)}`,
        jsonRequest("PUT", selected)
      );
      drawProject();
      setStatus(`Bound ${selected.profile_id}@${selected.revision}.`, "success");
    } catch (error) {
      setStatus(error.message || "Production profile could not be bound.", "error");
    } finally {
      bindButton.disabled = false;
      updateUnbindAvailability();
    }
  });

  unbindButton.addEventListener("click", async () => {
    unbindButton.disabled = true;
    try {
      const projectId = currentProjectId();
      if (!projectId) throw new Error("Choose or paste a project ID.");
      loadedProject = await apiJson(
        `production-profiles/projects/${encodeURIComponent(projectId)}`,
        { method: "DELETE" }
      );
      drawProject();
      setStatus("Production profile removed; only PR25-owned defaults were restored.", "success");
    } catch (error) {
      setStatus(error.message || "Production profile could not be removed.", "error");
    } finally {
      updateUnbindAvailability();
    }
  });

  projectInput.addEventListener("input", () => {
    const projectId = currentProjectId();
    if (loadedProject && loadedProject.project_id !== projectId) loadedProject = null;
    drawProject();
  });

  async function refresh() {
    const bearer = await token();
    setHidden(panel, !bearer);
    if (!bearer) {
      profiles = [];
      loadedProject = null;
      registryList.replaceChildren();
      projectView.replaceChildren();
      count.textContent = "0";
      unbindButton.disabled = true;
      return;
    }
    try {
      await Promise.all([refreshRegistry(), refreshRecentProjectOptions()]);
      drawProject();
      if (status.dataset.kind !== "error") {
        setStatus(`${profiles.length} reusable production profile(s) available.`);
      }
    } catch (error) {
      profiles = [];
      drawRegistry();
      updateUnbindAvailability();
      setStatus(`${error.message || "Production profiles could not be loaded."} Manual exact profile ID + revision rebind remains available from Bind / rebind.`, "error");
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
