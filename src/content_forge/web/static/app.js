"use strict";

const API_BASE = new URL("../api/v1/", window.location.href);

const elements = {
  connectionBadge: document.getElementById("connection-badge"), pairForm: document.getElementById("pair-form"), challengeId: document.getElementById("challenge-id"), challengeCode: document.getElementById("challenge-code"), pairStatus: document.getElementById("pair-status"), pairedActions: document.getElementById("paired-actions"), revokeButton: document.getElementById("revoke-button"), installButton: document.getElementById("install-button"), refreshButton: document.getElementById("refresh-button"), desktopOnboarding: document.getElementById("desktop-onboarding"), onboardingForm: document.getElementById("onboarding-form"), publicUrl: document.getElementById("public-url"), onboardingResult: document.getElementById("onboarding-result"), pairingQr: document.getElementById("pairing-qr"), pairingAddress: document.getElementById("pairing-address"), onboardingStatus: document.getElementById("onboarding-status"), capturePanel: document.getElementById("capture-panel"), fileInput: document.getElementById("file-input"), noteForm: document.getElementById("note-form"), noteUrl: document.getElementById("note-url"), noteText: document.getElementById("note-text"), queueBadge: document.getElementById("queue-badge"), retryQueueButton: document.getElementById("retry-queue-button"), captureStatus: document.getElementById("capture-status"), progressShell: document.getElementById("progress-shell"), progressBar: document.getElementById("progress-bar"), progressLabel: document.getElementById("progress-label"), inboxPanel: document.getElementById("inbox-panel"), inboxList: document.getElementById("inbox-list"), inboxCount: document.getElementById("inbox-count"), inboxStatus: document.getElementById("inbox-status"), emptyTemplate: document.getElementById("empty-template")
};

let bearerToken = null;
let installPrompt = null;
let queueDraining = false;
let thumbnailUrls = [];

function setStatus(element, message, kind) { element.textContent = message || ""; element.dataset.kind = kind || ""; }
function setHidden(element, hidden) { element.classList.toggle("hidden", Boolean(hidden)); }
function setPairedState(paired) {
  elements.connectionBadge.textContent = paired ? "Paired" : "Not paired";
  elements.connectionBadge.className = paired ? "badge success" : "badge neutral";
  setHidden(elements.pairForm, paired); setHidden(elements.pairedActions, !paired); setHidden(elements.capturePanel, !paired); setHidden(elements.inboxPanel, !paired);
}
function isLoopbackHostname(hostname) { const value = String(hostname || "").toLowerCase(); return value === "localhost" || value === "127.0.0.1" || value === "::1" || value === "[::1]"; }
async function safeJson(response) { try { return await response.json(); } catch (_) { return {}; } }
async function apiFetchWithBearer(relativePath, options) {
  const requestBearer = bearerToken;
  const requestOptions = Object.assign({}, options || {}); requestOptions.headers = new Headers(requestOptions.headers || {});
  if (requestBearer) requestOptions.headers.set("Authorization", `Bearer ${requestBearer}`);
  try {
    const response = await fetch(new URL(relativePath, API_BASE), requestOptions);
    return { response, requestBearer };
  } catch (error) {
    error.requestBearer = requestBearer;
    throw error;
  }
}
async function apiFetch(relativePath, options) { return (await apiFetchWithBearer(relativePath, options)).response; }

async function revokeIssuedPairingToken(token) {
  try {
    const response = await fetch(new URL("sessions/current", API_BASE), {
      method: "DELETE",
      headers: { "Authorization": `Bearer ${token}` },
    });
    // A 401 also proves this credential is not a usable live server session anymore.
    return response.ok || response.status === 401;
  } catch (_) {
    return false;
  }
}

async function exchangePairing(challengeId, code) {
  const response = await fetch(new URL("pairing/exchange", API_BASE), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ challenge_id: challengeId, code, label: `PWA ${navigator.userAgent.slice(0, 120)}` }) });
  if (!response.ok) { const payload = await safeJson(response); throw new Error(payload.detail || `Pairing failed (${response.status})`); }
  const payload = await response.json();
  const issuedToken = payload.token;
  try {
    // Persistence must succeed before the newly issued bearer becomes ordinary paired
    // state. Otherwise a reload could discard the only local copy of a live session.
    await window.CFStore.setToken(issuedToken);
  } catch (storageError) {
    // The server session already exists. Expose its only credential immediately while
    // automatic cleanup is attempted, so a stalled DELETE can still be revoked through
    // the visible Disconnect action rather than trapping the bearer in this closure.
    bearerToken = issuedToken;
    setPairedState(true);
    setStatus(elements.pairStatus, `${storageError.message || "Could not persist the pairing session."} Attempting automatic revocation; Disconnect remains available while cleanup is pending.`, "error");
    const revoked = await revokeIssuedPairingToken(issuedToken);
    if (revoked) {
      if (bearerToken === issuedToken) {
        await finalizeInvalidatedSession(
          "Issued pairing session was revoked after local persistence failed.",
          "error",
          issuedToken
        );
      }
      throw new Error(`${storageError.message || "Could not persist the pairing session."} The issued server session was revoked.`);
    }
    if (bearerToken === issuedToken) {
      throw new Error(`${storageError.message || "Could not persist the pairing session."} Automatic revocation also failed; this page still holds the live session. Use Disconnect before closing or reloading.`);
    }
    throw new Error(`${storageError.message || "Could not persist the pairing session."} The issued session changed while automatic cleanup was pending.`);
  }
  bearerToken = issuedToken;
  setPairedState(true);
  setStatus(elements.pairStatus, "Device paired.", "success");
}

async function handlePairForm(event) {
  event.preventDefault(); setStatus(elements.pairStatus, "Pairing…");
  try { await exchangePairing(elements.challengeId.value.trim(), elements.challengeCode.value.trim()); elements.challengeCode.value = ""; await drainQueue(); await loadInbox(); }
  catch (error) { setStatus(elements.pairStatus, error.message || "Pairing failed.", "error"); }
}

async function autoPairFromFragment() {
  if (!window.location.hash) return false;
  const params = new URLSearchParams(window.location.hash.slice(1)); const challengeId = params.get("challenge_id"); const code = params.get("code");
  if (!challengeId || !code) return false;
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  if (bearerToken) {
    setStatus(elements.pairStatus, "This device is already paired. Disconnect the current session before pairing another QR.", "error");
    return false;
  }
  elements.challengeId.value = challengeId; elements.challengeCode.value = code; setStatus(elements.pairStatus, "Pairing from QR…");
  try { await exchangePairing(challengeId, code); await drainQueue(); await loadInbox(); return true; }
  catch (error) { setStatus(elements.pairStatus, error.message || "QR pairing failed.", "error"); return false; }
}

async function finalizeInvalidatedSession(message, kind, expectedBearer) {
  // A late response from an old request must never revoke a newer pairing. Require the
  // bearer that actually authorized the request to still be current before changing UI
  // state, then use a compare-and-delete IndexedDB transaction so a new token written
  // while cleanup is pending cannot be erased by the old request either.
  const invalidatedBearer = expectedBearer || bearerToken;
  if (!invalidatedBearer || bearerToken !== invalidatedBearer) return false;
  bearerToken = null;
  clearThumbnailUrls();
  setPairedState(false);
  try {
    await window.CFStore.clearTokenIfMatches(invalidatedBearer);
    if (bearerToken) return false;
    setStatus(elements.pairStatus, message, kind || "success");
    return true;
  } catch (error) {
    if (bearerToken) return false;
    setStatus(elements.pairStatus, `${message} ${error.message || "Local token cleanup failed."} The server session is no longer usable, but its stale token may remain in browser storage; reload may repeat this cleanup warning.`, "error");
    return false;
  }
}

async function revokeSession() {
  if (!bearerToken) return;
  setStatus(elements.pairStatus, "Revoking device session…");
  let result;
  try {
    result = await apiFetchWithBearer("sessions/current", { method: "DELETE" });
  } catch (error) {
    if (error.requestBearer !== bearerToken) return;
    setStatus(elements.pairStatus, `${error.message || "Revocation failed."} Session retained so revocation can be retried.`, "error");
    return;
  }
  const { response, requestBearer } = result;
  if (requestBearer !== bearerToken) return;
  if (!response.ok && response.status !== 401) {
    const payload = await safeJson(response);
    if (requestBearer !== bearerToken) return;
    setStatus(elements.pairStatus, `${payload.detail || `Revocation failed (${response.status})`} Session retained so revocation can be retried.`, "error");
    return;
  }
  await finalizeInvalidatedSession(
    response.status === 401 ? "Device session was already inactive. Local pairing cleared." : "Device disconnected.",
    "success",
    requestBearer
  );
}

function clearThumbnailUrls() { for (const url of thumbnailUrls) URL.revokeObjectURL(url); thumbnailUrls = []; }
async function fetchThumbnail(assetId, img) {
  try {
    const { response, requestBearer } = await apiFetchWithBearer(`assets/${encodeURIComponent(assetId)}/thumbnail`);
    if (!response.ok) return;
    const blob = await response.blob();
    if (requestBearer !== bearerToken) return;
    const objectUrl = URL.createObjectURL(blob); thumbnailUrls.push(objectUrl); img.src = objectUrl; img.classList.remove("hidden");
  } catch (_) {}
}
function formatBytes(value) { const bytes = Number(value); if (!Number.isFinite(bytes) || bytes < 0) return ""; const units = ["B","KB","MB","GB"]; let amount = bytes, index = 0; while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; } return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`; }
function formatDate(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(); }

function createCard(item) {
  const article = document.createElement("article"); article.className = "inbox-card";
  const media = document.createElement("div"); media.className = "card-media"; const img = document.createElement("img"); img.className = "thumbnail hidden"; img.alt = ""; media.appendChild(img);
  const body = document.createElement("div"); body.className = "card-body"; const heading = document.createElement("div"); heading.className = "card-heading"; const title = document.createElement("strong"); title.textContent = item.original_name || item.source_url || "Inbox capture"; const badge = document.createElement("span"); badge.className = `badge state-${String(item.state || "unknown")}`; badge.textContent = String(item.state || "unknown"); heading.append(title, badge);
  const meta = document.createElement("p"); meta.className = "muted compact-text"; const details = []; if (item.kind) details.push(String(item.kind)); if (item.size_bytes != null) details.push(formatBytes(item.size_bytes)); if (item.updated_at) details.push(formatDate(item.updated_at)); meta.textContent = details.join(" · "); body.append(heading, meta);
  if (item.note) { const note = document.createElement("p"); note.className = "card-note"; note.textContent = item.note; body.appendChild(note); }
  if (item.source_url) { const source = document.createElement("p"); source.className = "mono compact-text wrap"; source.textContent = item.source_url; body.appendChild(source); }
  if (item.error_message) { const error = document.createElement("p"); error.className = "error-text"; error.textContent = item.error_message; body.appendChild(error); }
  article.append(media, body); if (item.asset_id && item.thumbnail_state === "succeeded") fetchThumbnail(item.asset_id, img); return article;
}

async function loadInbox() {
  if (!bearerToken) return; setStatus(elements.inboxStatus, "Loading…");
  let requestBearer = bearerToken;
  try {
    const result = await apiFetchWithBearer("inbox?limit=100");
    const response = result.response; requestBearer = result.requestBearer;
    if (response.status === 401) { await finalizeInvalidatedSession("Session expired. Pair again.", "error", requestBearer); return; }
    if (requestBearer !== bearerToken) return;
    if (!response.ok) throw new Error(`Inbox request failed (${response.status})`);
    const payload = await response.json();
    if (requestBearer !== bearerToken) return;
    const items = Array.isArray(payload.items) ? payload.items : []; clearThumbnailUrls(); elements.inboxList.replaceChildren();
    if (!items.length) elements.inboxList.appendChild(elements.emptyTemplate.content.cloneNode(true)); else for (const item of items) elements.inboxList.appendChild(createCard(item));
    elements.inboxCount.textContent = String(items.length); setStatus(elements.inboxStatus, "");
  } catch (error) {
    if (requestBearer !== bearerToken) return;
    setStatus(elements.inboxStatus, error.message || "Could not load Inbox.", "error");
  }
}

async function updateQueueBadge() { const queued = await window.CFStore.listShares(); elements.queueBadge.textContent = `Queue ${queued.length}`; return queued; }
function showProgress(label, fraction) { setHidden(elements.progressShell, false); const normalized = Math.max(0, Math.min(1, Number(fraction) || 0)); elements.progressBar.style.width = `${Math.round(normalized * 100)}%`; elements.progressLabel.textContent = label; }
function hideProgressSoon() { window.setTimeout(() => { setHidden(elements.progressShell, true); elements.progressBar.style.width = "0%"; }, 700); }
function isPermanentQueueRejection(status) { const value = Number(status); return value >= 400 && value < 500 && ![401, 408, 425, 429].includes(value); }

function uploadQueuedFile(record) {
  return new Promise((resolve, reject) => {
    const requestBearer = bearerToken;
    const rejectRequest = (message, status) => {
      const error = new Error(message); error.status = status; error.requestBearer = requestBearer; reject(error);
    };
    if (!requestBearer) { rejectRequest("Pairing session is unavailable.", 401); return; }
    const xhr = new XMLHttpRequest(); xhr.open("POST", new URL("inbox/files", API_BASE)); xhr.setRequestHeader("Authorization", `Bearer ${requestBearer}`); xhr.setRequestHeader("Idempotency-Key", record.id); xhr.responseType = "json";
    xhr.upload.onprogress = (event) => { showProgress(`Uploading ${record.originalName || "file"}…`, event.lengthComputable ? event.loaded / event.total : 0); };
    xhr.onload = () => { if (xhr.status >= 200 && xhr.status < 300) { showProgress(`Accepted ${record.originalName || "file"}`, 1); resolve(xhr.response || {}); } else { rejectRequest((xhr.response && xhr.response.detail) || `Upload failed (${xhr.status})`, xhr.status); } };
    xhr.onerror = () => rejectRequest("Network error while uploading."); xhr.onabort = () => rejectRequest("Upload cancelled.");
    const form = new FormData(); const file = record.file; form.append("file", file, record.originalName || file.name || "shared-file"); if (record.sourceUrl) form.append("source_url", record.sourceUrl); if (record.note) form.append("note", record.note); xhr.send(form);
  });
}

async function uploadQueuedNote(record) {
  const { response, requestBearer } = await apiFetchWithBearer("inbox/url-note", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": record.id }, body: JSON.stringify({ source_url: record.sourceUrl || null, note: record.note || null }) });
  if (!response.ok) { const payload = await safeJson(response); const error = new Error(payload.detail || `Capture failed (${response.status})`); error.status = response.status; error.requestBearer = requestBearer; throw error; }
  return response.json();
}

async function drainQueue() {
  if (!bearerToken || queueDraining) { await updateQueueBadge(); return; }
  queueDraining = true;
  let redrainForNewBearer = false;
  try {
    const queued = await updateQueueBadge();
    for (const record of queued) {
      try {
        if (record.kind === "file") await uploadQueuedFile(record); else await uploadQueuedNote(record);
        await window.CFStore.deleteShare(record.id);
        await updateQueueBadge();
      } catch (error) {
        if (error.status === 401) {
          const requestBearer = error.requestBearer || null;
          if (requestBearer && requestBearer !== bearerToken) {
            if (bearerToken) redrainForNewBearer = true;
            else break;
            continue;
          }
          await finalizeInvalidatedSession("Session expired. Pair again; queued shares are preserved.", "error", requestBearer || bearerToken);
          break;
        }
        if (error.status === 413) {
          setStatus(elements.captureStatus, `${error.message || "Capture exceeds the current server upload limit."} The item remains queued; refresh Content Forge to load current limits. Later captures will continue.`, "error");
          continue;
        }
        if (isPermanentQueueRejection(error.status)) {
          await window.CFStore.deleteShare(record.id);
          await updateQueueBadge();
          setStatus(elements.captureStatus, `${error.message || "Capture rejected."} Removed from the retry queue so later captures can continue.`, "error");
          continue;
        }
        setStatus(elements.captureStatus, `${error.message || "Capture failed."} The item remains queued for retry.`, "error");
        break;
      }
    }
  } finally { queueDraining = false; hideProgressSoon(); }
  if (bearerToken) await loadInbox();
  if (redrainForNewBearer && bearerToken) await drainQueue();
}

async function queueFiles(files) {
  const records = files.map((file) => ({
    kind: "file",
    file,
    originalName: file.name || "upload",
    mimeType: file.type || "application/octet-stream",
    sourceUrl: null,
    note: null,
  }));
  try {
    await window.CFStore.enqueueShares(records);
  } catch (error) {
    setStatus(elements.captureStatus, `${error.message || "Could not queue selected files."} Nothing was queued; the current selection is still available.`, "error");
    return false;
  }
  elements.fileInput.value = "";
  try {
    await updateQueueBadge();
    await drainQueue();
  } catch (error) {
    setStatus(elements.captureStatus, `${error.message || "The files were queued, but the queue view could not be refreshed."} The queued capture is preserved.`, "error");
  }
  return true;
}

async function saveNote(event) {
  event.preventDefault();
  const sourceUrl = elements.noteUrl.value.trim(), note = elements.noteText.value.trim();
  if (!sourceUrl && !note) { setStatus(elements.captureStatus, "Enter a URL or note.", "error"); return; }
  try {
    await window.CFStore.enqueueShare({ kind: "url_note", sourceUrl: sourceUrl || null, note: note || null });
  } catch (error) {
    setStatus(elements.captureStatus, `${error.message || "Could not queue the URL/note."} Nothing was queued; your form values were kept.`, "error");
    return;
  }
  elements.noteUrl.value = ""; elements.noteText.value = "";
  try {
    await updateQueueBadge();
    await drainQueue();
  } catch (error) {
    setStatus(elements.captureStatus, `${error.message || "The URL/note was queued, but the queue view could not be refreshed."} The queued capture is preserved.`, "error");
  }
}

async function createOnboarding(event) {
  event.preventDefault(); setStatus(elements.onboardingStatus, "Creating pairing QR…");
  try {
    const endpoint = new URL("pairing/challenges", API_BASE); endpoint.searchParams.set("public_url", elements.publicUrl.value.trim()); const response = await fetch(endpoint, { method: "POST" });
    if (!response.ok) { const payload = await safeJson(response); throw new Error(payload.detail || `Could not create pairing QR (${response.status})`); }
    const payload = await response.json(); const svgBlob = new Blob([payload.qr_svg], { type: "image/svg+xml" }); const old = elements.pairingQr.dataset.objectUrl; if (old) URL.revokeObjectURL(old); const objectUrl = URL.createObjectURL(svgBlob); elements.pairingQr.dataset.objectUrl = objectUrl; elements.pairingQr.src = objectUrl; elements.pairingAddress.textContent = payload.pairing_url; setHidden(elements.onboardingResult, false); setStatus(elements.onboardingStatus, `Pairing code ${payload.code} expires soon.`, "success");
  } catch (error) { setStatus(elements.onboardingStatus, error.message || "Could not create onboarding QR.", "error"); }
}

async function registerServiceWorker() { if (!("serviceWorker" in navigator)) { setStatus(elements.captureStatus, "This browser does not support Service Workers.", "error"); return; } try { await navigator.serviceWorker.register("sw.js", { scope: "./", updateViaCache: "none" }); } catch (error) { setStatus(elements.captureStatus, `PWA worker registration failed: ${error.message}`, "error"); } }
function wireInstallPrompt() { window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); installPrompt = event; setHidden(elements.installButton, false); }); elements.installButton.addEventListener("click", async () => { if (!installPrompt) return; installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; setHidden(elements.installButton, true); }); }

async function initialize() {
  elements.pairForm.addEventListener("submit", handlePairForm); elements.revokeButton.addEventListener("click", revokeSession); elements.refreshButton.addEventListener("click", async () => { await drainQueue(); await loadInbox(); }); elements.fileInput.addEventListener("change", async () => { const files = Array.from(elements.fileInput.files || []); if (files.length) await queueFiles(files); }); elements.noteForm.addEventListener("submit", saveNote); elements.retryQueueButton.addEventListener("click", drainQueue); elements.onboardingForm.addEventListener("submit", createOnboarding); wireInstallPrompt(); setHidden(elements.desktopOnboarding, !isLoopbackHostname(window.location.hostname)); await registerServiceWorker(); bearerToken = await window.CFStore.getToken(); setPairedState(Boolean(bearerToken)); await updateQueueBadge(); const pairedFromQr = await autoPairFromFragment(); if (!pairedFromQr && bearerToken) { await drainQueue(); await loadInbox(); } if (new URLSearchParams(window.location.search).has("share_error")) setStatus(elements.captureStatus, "The shared item could not be queued. Try sharing again.", "error"); window.setInterval(() => { if (bearerToken && document.visibilityState === "visible" && !queueDraining) loadInbox(); }, 7000);
}

initialize().catch((error) => setStatus(elements.pairStatus, error.message || "Application initialization failed.", "error"));
