"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const REFRESH_EVENT = "content-forge:production-home-refreshed";
  const GROUP_ORDER = Object.freeze([
    ["failed", "Needs recovery", "Fix uncertain or failed work before making replacements."],
    ["attention", "Needs you", "Only decisions or explicit side effects that require human authority."],
    ["safe_work", "Ready automatically", "The desktop can continue these without accepting a human decision."],
    ["working", "Working", "Rendering, QC, or remote execution is already in progress."],
    ["inbox", "New sources", "Unused captures ready to become a video."],
    ["finished", "Finished", "Completed local finals or durable successful publications."],
  ]);

  const projectList = document.getElementById("production-home-projects");
  const summary = document.getElementById("production-home-summary");
  const count = document.getElementById("production-home-count");
  const status = document.getElementById("production-home-status");
  const createVideoButton = document.getElementById("production-home-create-video");
  if (!projectList || !summary || !count || !status || !createVideoButton) return;

  let refreshGeneration = 0;
  let runningSafeWork = false;

  function text(tag, value, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value == null ? "" : String(value);
    return node;
  }

  function setStatus(message, kind) {
    status.textContent = message || "";
    status.dataset.kind = kind || "";
  }

  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }

  async function apiJson(relativePath, options) {
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
    return response.json();
  }

  function button(label, className, handler) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = className || "secondary";
    node.textContent = label;
    node.addEventListener("click", async () => {
      node.disabled = true;
      try { await handler(); }
      catch (error) {
        setStatus(error && error.message ? error.message : "Production action failed.", "error");
      } finally {
        node.disabled = false;
      }
    });
    return node;
  }

  function projectLabel(project) {
    if (project && typeof project.production_preset_label === "string" && project.production_preset_label.trim()) {
      const sources = Number(project.production_source_count || 0);
      return sources > 1
        ? `${project.production_preset_label} · ${sources} sources`
        : project.production_preset_label;
    }
    const kind = String(project && project.content_kind || "Video project").replaceAll("_", " ");
    return kind.charAt(0).toUpperCase() + kind.slice(1);
  }

  function groupBadge(group) {
    if (group === "failed") return ["Recovery", "badge state-failed"];
    if (group === "attention") return ["Needs you", "badge state-partial"];
    if (group === "safe_work") return ["Automatic", "badge state-receiving"];
    if (group === "working") return ["Working", "badge state-receiving"];
    if (group === "finished") return ["Finished", "badge success"];
    return ["Source", "badge neutral"];
  }

  function renderProjectCard(item) {
    const project = item.project || {};
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.append(
      text("strong", projectLabel(project)),
      text("p", item.reason || "Continue production", "muted compact-text")
    );
    const [badge, badgeClass] = groupBadge(item.group);
    heading.append(left, text("span", badge, badgeClass));
    card.appendChild(heading);

    const stateBits = [];
    if (project.state) stateBits.push(String(project.state).replaceAll("_", " "));
    if (item.publish_state) stateBits.push(`publish: ${item.publish_state}`);
    if (stateBits.length) card.appendChild(text("p", stateBits.join(" · "), "muted compact-text"));

    const actions = document.createElement("div");
    actions.className = "review-actions";
    actions.appendChild(button(
      item.group === "finished" ? "View" : "Open project",
      item.group === "attention" || item.group === "failed" ? "primary" : "secondary",
      async () => {
        const api = window.CFProductionHome;
        if (!api || typeof api.openProject !== "function") throw new Error("Project navigation is unavailable.");
        await api.openProject(project.project_id, projectLabel(project));
      }
    ));
    card.appendChild(actions);
    return card;
  }

  function renderSourceCard(item) {
    const source = item.source || {};
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    left.append(
      text("strong", source.label || "Media source"),
      text("p", item.reason || "Ready for Create video", "muted compact-text")
    );
    heading.append(left, text("span", String(source.media_type || "source").toUpperCase(), "badge neutral"));
    card.appendChild(heading);
    if (source.media_type === "video" && Number(source.duration_seconds) > 0) {
      card.appendChild(text("p", `${Number(source.duration_seconds).toFixed(1)} sec`, "muted compact-text"));
    }
    const actions = document.createElement("div");
    actions.className = "review-actions";
    actions.appendChild(button("Create video", "primary", async () => {
      const api = window.CFProductionHome;
      if (!api || typeof api.openCreateVideo !== "function") throw new Error("Create video is unavailable.");
      await api.openCreateVideo();
    }));
    card.appendChild(actions);
    return card;
  }

  function renderIntakeCard(item) {
    const intake = item.intake || {};
    const card = document.createElement("article");
    card.className = "review-card";
    const heading = document.createElement("div");
    heading.className = "card-heading";
    const left = document.createElement("div");
    const label = intake.filename || intake.source_url || intake.kind || "Captured item";
    left.append(
      text("strong", label),
      text("p", item.reason || "Capture needs attention", "muted compact-text")
    );
    const [badge, badgeClass] = groupBadge(item.group);
    heading.append(left, text("span", badge, badgeClass));
    card.appendChild(heading);
    if (intake.error_code) card.appendChild(text("p", `Error: ${intake.error_code}`, "muted compact-text"));
    if (intake.project_id) {
      const actions = document.createElement("div");
      actions.className = "review-actions";
      actions.appendChild(button("Open project", "secondary", async () => {
        const api = window.CFProductionHome;
        if (!api || typeof api.openProject !== "function") throw new Error("Project navigation is unavailable.");
        await api.openProject(intake.project_id, "Captured item");
      }));
      card.appendChild(actions);
    }
    return card;
  }

  function renderCard(item) {
    if (item.kind === "project") return renderProjectCard(item);
    if (item.kind === "source") return renderSourceCard(item);
    return renderIntakeCard(item);
  }

  function renderSummary(payload) {
    summary.replaceChildren();
    const counts = payload.counts || {};
    const chips = [
      ["Needs you", Number(counts.attention || 0)],
      ["Automatic", Number(counts.safe_work || 0)],
      ["Working", Number(counts.working || 0)],
      ["Recovery", Number(counts.failed || 0)],
      ["New", Number(counts.inbox || 0)],
      ["Finished", Number(counts.finished || 0)],
    ];
    for (const [label, value] of chips) {
      const chip = document.createElement("span");
      chip.className = "badge neutral";
      chip.textContent = `${label}: ${value}`;
      summary.appendChild(chip);
    }
  }

  function renderQueue(payload) {
    const items = Array.isArray(payload.items) ? payload.items : [];
    projectList.replaceChildren();
    renderSummary(payload);
    count.textContent = String(payload.total == null ? items.length : payload.total);

    for (const [group, label, help] of GROUP_ORDER) {
      const grouped = items.filter((item) => item && item.group === group);
      if (!grouped.length) continue;
      const section = document.createElement("section");
      section.className = "stack compact";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.append(text("strong", label), text("p", help, "muted compact-text"));
      heading.append(left, text("span", String(grouped.length), "badge neutral"));
      section.appendChild(heading);
      const list = document.createElement("div");
      list.className = "review-list";
      for (const item of grouped) list.appendChild(renderCard(item));
      section.appendChild(list);
      projectList.appendChild(section);
    }

    if (!items.length) {
      projectList.appendChild(text("div", "No source or production work needs attention.", "empty-state"));
    }
    if (payload.truncated) {
      projectList.appendChild(text("p", "Showing a bounded daily queue. Use Advanced surfaces for older history.", "muted compact-text"));
    }
  }

  async function refreshAttention() {
    const generation = ++refreshGeneration;
    const bearer = await token();
    if (!bearer) return;
    try {
      // PR27 reconciliation remains owned by the publishing surface. Calling status first
      // makes stale pre-restart running attempts outcome_unknown before the read-only
      // daily projection ranks them.
      await apiJson("publishing/status");
      const payload = await apiJson("production/attention?limit=100");
      if (generation !== refreshGeneration) return;
      renderQueue(payload);
      const counts = payload.counts || {};
      if (Number(counts.failed || 0) > 0) {
        setStatus("Recovery items are shown first. Safe work will not retry uncertain remote outcomes.", "error");
      } else if (Number(counts.attention || 0) > 0) {
        setStatus("Only the decisions that need you are at the top of the queue.");
      } else if (Number(counts.safe_work || 0) > 0) {
        setStatus("Safe desktop work is ready to continue without accepting human decisions.");
      } else {
        setStatus("Daily production queue is clear.", "success");
      }
    } catch (error) {
      if (generation !== refreshGeneration) return;
      setStatus(error && error.message ? error.message : "Daily attention queue could not be refreshed.", "error");
    }
  }

  const safeWorkButton = document.createElement("button");
  safeWorkButton.id = "production-home-safe-work";
  safeWorkButton.type = "button";
  safeWorkButton.className = "secondary";
  safeWorkButton.textContent = "Run safe work";
  createVideoButton.insertAdjacentElement("afterend", safeWorkButton);
  safeWorkButton.addEventListener("click", async () => {
    if (runningSafeWork) return;
    runningSafeWork = true;
    safeWorkButton.disabled = true;
    setStatus("Desktop is continuing only bounded deterministic work…");
    try {
      const result = await apiJson("production/safe-work", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ render_limit: 4 }),
      });
      const failures = Array.isArray(result.results)
        ? result.results.filter((item) => item && item.outcome === "failed").length
        : 0;
      setStatus(
        `Safe work finished: ${Number(result.prepared || 0)} prepared, ${Number(result.rendered || 0)} render operation(s), ${failures} failed.`,
        failures ? "error" : "success"
      );
      const api = window.CFProductionHome;
      if (api && typeof api.refreshHome === "function") await api.refreshHome();
      else await refreshAttention();
    } finally {
      runningSafeWork = false;
      safeWorkButton.disabled = false;
    }
  });

  window.addEventListener(REFRESH_EVENT, () => void refreshAttention());
})();
