importScripts("config.js", "shared.js");

"use strict";

const CACHE_PREFIX = `content-forge-shell:${self.registration.scope}:`;
const CACHE_NAME = `${CACHE_PREFIX}v3`;
const LIMITS = self.CFStore.limits;
const ALLOWED_FIELDS = new Set(["title", "text", "url", "files"]);

function appUrl(relative) {
  return new URL(relative, self.registration.scope).href;
}

const SHELL_ASSETS = [
  appUrl("./"),
  appUrl("config.js"),
  appUrl("styles.css"),
  appUrl("shared.js"),
  appUrl("app.js"),
  appUrl("manifest.webmanifest"),
  appUrl("icons/icon-192.png"),
  appUrl("icons/icon-512.png"),
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

function normalizeString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function shareRequestIsTrustedNavigation(request) {
  const fetchSite = request.headers.get("sec-fetch-site");
  return request.mode === "navigate"
    && request.destination === "document"
    && (fetchSite === "none" || fetchSite === "same-origin");
}

function boundedContentLength(request) {
  const raw = request.headers.get("content-length");
  if (!raw || !/^\d+$/.test(raw)) throw new Error("share target requires Content-Length");
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0 || value > LIMITS.maxShareBodyBytes) {
    throw new Error("shared payload exceeds the local queue limit");
  }
  return value;
}

async function queueShareTarget(request) {
  if (!shareRequestIsTrustedNavigation(request)) throw new Error("share target provenance rejected");
  const contentType = String(request.headers.get("content-type") || "").toLowerCase();
  if (!contentType.startsWith("multipart/form-data;")) throw new Error("share target requires multipart form data");
  boundedContentLength(request);

  // A native share may be queued while the server is offline, but only a browser profile
  // that has already completed PR8 pairing may parse/persist the OS-provided multipart.
  const token = await self.CFStore.getToken();
  if (!token) throw new Error("share target requires a paired device");

  // Reject already-full queues before multipart parsing. Exact batch/count/byte checks
  // still happen atomically in CFStore at the persistence boundary after parsing.
  const usage = await self.CFStore.queueUsage();
  if (usage.entries >= LIMITS.maxQueueEntries || usage.fileBytes > LIMITS.maxQueueBytes) {
    throw new Error("share queue is full");
  }

  const data = await request.formData();
  for (const key of data.keys()) {
    if (!ALLOWED_FIELDS.has(key)) throw new Error("share target contains an unsupported field");
  }

  const title = normalizeString(data.get("title"));
  const text = normalizeString(data.get("text"));
  const url = normalizeString(data.get("url"));
  const rawFiles = data.getAll("files");
  if (rawFiles.length > LIMITS.maxBatchEntries) throw new Error("too many shared files");
  if (rawFiles.some((item) => !(item instanceof File))) throw new Error("invalid shared file field");
  const note = [title, text].filter(Boolean).join("\n");

  if (url.length > LIMITS.maxUrlChars) throw new Error("shared URL is too long");
  if (note.length > LIMITS.maxNoteChars) throw new Error("shared note is too long");
  if (!rawFiles.length && !url && !note) throw new Error("empty share target payload");

  const records = rawFiles.length
    ? rawFiles.map((file) => ({
        kind: "file",
        file,
        originalName: file.name || "shared-file",
        mimeType: file.type || "application/octet-stream",
        sourceUrl: url || null,
        note: note || null,
      }))
    : [{
        kind: "url_note",
        sourceUrl: url || null,
        note: note || null,
      }];

  // One IndexedDB transaction owns validation plus all inserts, so a multi-file Android
  // share is either fully queued or not queued at all. Retrying after a storage error
  // therefore cannot duplicate an already-committed prefix of the same OS share.
  await self.CFStore.enqueueShares(records);
  return Response.redirect(appUrl("./?shared=1"), 303);
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  const shareTargetPath = new URL("share-target", self.registration.scope).pathname;

  if (request.method === "POST" && url.origin === self.location.origin && url.pathname === shareTargetPath) {
    event.respondWith(
      queueShareTarget(request).catch(() => Response.redirect(appUrl("./?share_error=1"), 303))
    );
    return;
  }

  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  const scopePath = new URL(self.registration.scope).pathname;
  if (!url.pathname.startsWith(scopePath)) return;

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (response.ok && response.type === "basic") {
          const copy = response.clone();
          event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)));
        }
        return response;
      }).catch(async () => {
        if (request.mode !== "navigate") throw new Error("offline resource unavailable");
        const cache = await caches.open(CACHE_NAME);
        const shell = await cache.match(appUrl("./"));
        if (!shell) throw new Error("offline shell unavailable");
        return shell;
      });
    })
  );
});
