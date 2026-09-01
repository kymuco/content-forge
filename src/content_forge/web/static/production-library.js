"use strict";

(() => {
  const API_BASE = new URL("../api/v1/", window.location.href);
  const panel = document.getElementById("production-library-panel");
  const count = document.getElementById("production-library-count");
  const status = document.getElementById("production-library-status");
  const searchForm = document.getElementById("production-library-search-form");
  const exactTagsInput = document.getElementById("production-library-search-tags");
  const prefixInput = document.getElementById("production-library-search-prefix");
  const usedSelect = document.getElementById("production-library-search-used");
  const results = document.getElementById("production-library-results");
  const collectionForm = document.getElementById("production-library-collection-form");
  const collectionIdInput = document.getElementById("production-library-collection-id");
  const collectionNameInput = document.getElementById("production-library-collection-name");
  const collections = document.getElementById("production-library-collections");
  const duplicateForm = document.getElementById("production-library-duplicate-form");
  const duplicateShaInput = document.getElementById("production-library-duplicate-sha");
  const duplicateView = document.getElementById("production-library-duplicate-view");

  if (!panel || !count || !status || !searchForm || !exactTagsInput || !prefixInput
      || !usedSelect || !results || !collectionForm || !collectionIdInput
      || !collectionNameInput || !collections || !duplicateForm || !duplicateShaInput
      || !duplicateView) return;

  const TAG_KINDS = new Set(["game", "anime", "artist", "character", "topic", "source"]);
  let currentQuery = { tags: [], limit: 50, offset: 0 };

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
  async function token() {
    try { return await window.CFStore.getToken(); } catch (_) { return null; }
  }
  async function apiJson(relativePath, options) {
    const bearer = await token();
    if (!bearer) throw new Error("Pair this device before browsing the production library.");
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

  function parseTagLines(value) {
    const tags = [];
    const seen = new Set();
    for (const rawLine of String(value || "").split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;
      const separator = line.indexOf(":");
      if (separator < 1) throw new Error(`Tag must use kind:value syntax: ${line}`);
      const kind = line.slice(0, separator).trim().toLowerCase();
      const tagValue = line.slice(separator + 1).trim();
      if (!TAG_KINDS.has(kind)) throw new Error(`Unsupported tag kind: ${kind}`);
      if (!tagValue) throw new Error(`Tag value is empty for ${kind}.`);
      const key = `${kind}\u0000${tagValue.toLocaleLowerCase()}`;
      if (seen.has(key)) throw new Error(`Duplicate tag: ${kind}:${tagValue}`);
      seen.add(key);
      tags.push({ kind, value: tagValue });
    }
    return tags;
  }

  function queryFromForm() {
    const query = { tags: parseTagLines(exactTagsInput.value), limit: 50, offset: 0 };
    const prefix = prefixInput.value.trim();
    if (prefix) query.tag_prefix = prefix;
    if (usedSelect.value === "used") query.previously_used = true;
    if (usedSelect.value === "unused") query.previously_used = false;
    return query;
  }

  function tagLines(tags) {
    return (Array.isArray(tags) ? tags : [])
      .map((item) => `${item.kind}:${item.value}`)
      .join("\n");
  }

  function drawReuse(container, items) {
    container.replaceChildren();
    if (!items.length) {
      container.appendChild(text("p", "No current Project references.", "muted compact-text"));
      return;
    }
    for (const item of items) {
      container.appendChild(text(
        "p",
        `${item.project_id} · ${item.content_kind} · ${item.project_state} · ${item.role}`,
        "muted compact-text mono wrap"
      ));
    }
  }

  function drawResults(items) {
    results.replaceChildren();
    count.textContent = String(items.length);
    if (!items.length) {
      results.appendChild(text("div", "No assets match this virtual query.", "empty-state"));
      return;
    }
    for (const hit of items) {
      const asset = hit.asset || {};
      const card = document.createElement("article");
      card.className = "review-card";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.appendChild(text("strong", asset.asset_id || "asset"));
      left.appendChild(text(
        "p",
        `${asset.media_type || "media"} · ${asset.size_bytes || 0} bytes · ${String(asset.sha256 || "").slice(0, 12)}…`,
        "muted compact-text mono wrap"
      ));
      heading.appendChild(left);
      heading.appendChild(text(
        "span",
        hit.project_count > 0 ? `used ${hit.project_count}` : "unused",
        hit.project_count > 0 ? "badge success" : "badge neutral"
      ));
      card.appendChild(heading);

      const tags = Array.isArray(hit.tags) ? hit.tags : [];
      if (tags.length) {
        card.appendChild(text(
          "p",
          tags.map((tag) => `${tag.kind}:${tag.value}`).join(" · "),
          "muted compact-text wrap"
        ));
      }
      const warnings = [];
      if (hit.source_count > 1) warnings.push(`${hit.source_count} provenance records for identical bytes`);
      if (hit.project_count > 0) warnings.push(`used by ${hit.project_count} Project(s)`);
      if (warnings.length) card.appendChild(text("p", warnings.join(" · "), "status"));

      const editor = document.createElement("textarea");
      editor.rows = Math.max(2, tags.length);
      editor.maxLength = 8192;
      editor.value = tagLines(tags);
      editor.placeholder = "game:Genshin Impact\ncharacter:Raiden Shogun";
      editor.setAttribute("aria-label", `Tags for ${asset.asset_id}`);
      card.appendChild(editor);

      const actions = document.createElement("div");
      actions.className = "row";
      const saveButton = document.createElement("button");
      saveButton.type = "button";
      saveButton.className = "secondary";
      saveButton.textContent = "Replace tags";
      const reuseButton = document.createElement("button");
      reuseButton.type = "button";
      reuseButton.className = "ghost";
      reuseButton.textContent = "Reuse history";
      actions.appendChild(saveButton);
      actions.appendChild(reuseButton);
      card.appendChild(actions);

      const reuseView = document.createElement("div");
      reuseView.className = "review-list";
      card.appendChild(reuseView);

      saveButton.addEventListener("click", async () => {
        saveButton.disabled = true;
        try {
          const newTags = parseTagLines(editor.value);
          await apiJson(
            `production-library/assets/${encodeURIComponent(asset.asset_id)}/tags`,
            jsonRequest("PUT", { tags: newTags })
          );
          setStatus(`Tags replaced for ${asset.asset_id}.`, "success");
          await runSearch(currentQuery);
        } catch (error) {
          setStatus(error.message || "Tags could not be replaced.", "error");
        } finally {
          saveButton.disabled = false;
        }
      });

      reuseButton.addEventListener("click", async () => {
        reuseButton.disabled = true;
        try {
          const payload = await apiJson(
            `production-library/assets/${encodeURIComponent(asset.asset_id)}/reuse`
          );
          drawReuse(reuseView, Array.isArray(payload.items) ? payload.items : []);
        } catch (error) {
          setStatus(error.message || "Reuse history could not be loaded.", "error");
        } finally {
          reuseButton.disabled = false;
        }
      });
      results.appendChild(card);
    }
  }

  async function runSearch(query) {
    const payload = await apiJson("production-library/search", jsonRequest("POST", query));
    const items = Array.isArray(payload.items) ? payload.items : [];
    drawResults(items);
    setStatus(`${items.length} asset(s) matched.`, "success");
  }

  function drawCollections(items) {
    collections.replaceChildren();
    if (!items.length) {
      collections.appendChild(text("div", "No virtual collections yet.", "empty-state"));
      return;
    }
    for (const item of items) {
      const card = document.createElement("article");
      card.className = "review-card";
      const heading = document.createElement("div");
      heading.className = "card-heading";
      const left = document.createElement("div");
      left.appendChild(text("strong", item.name));
      left.appendChild(text("p", item.collection_id, "muted compact-text mono wrap"));
      heading.appendChild(left);
      heading.appendChild(text("span", "virtual", "badge neutral"));
      card.appendChild(heading);
      const actions = document.createElement("div");
      actions.className = "row";
      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "secondary";
      openButton.textContent = "Open collection";
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "ghost";
      deleteButton.textContent = "Delete saved query";
      actions.appendChild(openButton);
      actions.appendChild(deleteButton);
      card.appendChild(actions);
      openButton.addEventListener("click", async () => {
        openButton.disabled = true;
        try {
          const payload = await apiJson(
            `production-library/collections/${encodeURIComponent(item.collection_id)}/items`
          );
          drawResults(Array.isArray(payload.items) ? payload.items : []);
          setStatus(`Opened virtual collection ${item.name}.`, "success");
        } catch (error) {
          setStatus(error.message || "Collection could not be opened.", "error");
        } finally {
          openButton.disabled = false;
        }
      });
      deleteButton.addEventListener("click", async () => {
        deleteButton.disabled = true;
        try {
          await apiJson(
            `production-library/collections/${encodeURIComponent(item.collection_id)}`,
            { method: "DELETE" }
          );
          await refreshCollections();
          setStatus(`Deleted saved query ${item.collection_id}.`, "success");
        } catch (error) {
          setStatus(error.message || "Collection could not be deleted.", "error");
        } finally {
          deleteButton.disabled = false;
        }
      });
      collections.appendChild(card);
    }
  }

  async function refreshCollections() {
    const payload = await apiJson("production-library/collections");
    drawCollections(Array.isArray(payload.items) ? payload.items : []);
  }

  searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      currentQuery = queryFromForm();
      setStatus("Searching local production library…");
      await runSearch(currentQuery);
    } catch (error) {
      setStatus(error.message || "Library search failed.", "error");
    }
  });

  collectionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const collectionId = collectionIdInput.value.trim();
    const name = collectionNameInput.value.trim();
    if (!collectionId || !name) {
      setStatus("Collection ID and name are required.", "error");
      return;
    }
    try {
      currentQuery = queryFromForm();
      await apiJson(
        `production-library/collections/${encodeURIComponent(collectionId)}`,
        jsonRequest("PUT", { name, query: currentQuery })
      );
      await refreshCollections();
      setStatus(`Saved current query as ${collectionId}.`, "success");
    } catch (error) {
      setStatus(error.message || "Virtual collection could not be saved.", "error");
    }
  });

  duplicateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const digest = duplicateShaInput.value.trim();
    if (!digest) return;
    duplicateView.replaceChildren();
    try {
      const payload = await apiJson(`production-library/duplicates/${encodeURIComponent(digest)}`);
      if (!payload.match) {
        duplicateView.appendChild(text("p", "No existing Asset has this SHA-256.", "muted compact-text"));
        return;
      }
      duplicateView.appendChild(text(
        "p",
        `${payload.match.asset.asset_id} · ${payload.match.source_count} source(s) · ${payload.match.project_count} Project(s)`,
        "muted compact-text mono wrap"
      ));
    } catch (error) {
      setStatus(error.message || "Duplicate lookup failed.", "error");
    }
  });

  async function refresh() {
    const bearer = await token();
    setHidden(panel, !bearer);
    if (!bearer) {
      results.replaceChildren();
      collections.replaceChildren();
      duplicateView.replaceChildren();
      count.textContent = "0";
      return;
    }
    try {
      await Promise.all([refreshCollections(), runSearch(currentQuery)]);
    } catch (error) {
      setStatus(error.message || "Production library could not be loaded.", "error");
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
