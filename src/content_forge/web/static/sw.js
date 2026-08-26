importScripts("config.js", "shared.js");

"use strict";

const CACHE_PREFIX = `content-forge-shell:${self.registration.scope}:`;
const CACHE_NAME = `${CACHE_PREFIX}v4`;
const LIMITS = self.CFStore.limits;
const ALLOWED_FIELDS = new Set(["title", "text", "url", "files"]);

function appUrl(relative) {
  return new URL(relative, self.registration.scope).href;
}

const CONFIG_URL = appUrl("config.js");
const SHELL_ASSETS = [
  appUrl("./"),
  CONFIG_URL,
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

function rejectAdvertisedOversize(request) {
  const raw = request.headers.get("content-length");
  if (raw == null) return;
  if (!/^\d+$/.test(raw)) throw new Error("invalid shared Content-Length");
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0 || value > LIMITS.maxShareBodyBytes) {
    throw new Error("shared payload exceeds the local queue limit");
  }
}

async function boundedMultipartFormData(request, contentType) {
  if (!request.body) throw new Error("share target has no request body");
  const reader = request.body.getReader();
  const chunks = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) throw new Error("invalid share target body chunk");
      totalBytes += value.byteLength;
      if (totalBytes > LIMITS.maxShareBodyBytes) {
        try { await reader.cancel("share target body limit exceeded"); } catch (_) {}
        throw new Error("shared payload exceeds the local queue limit");
      }
      chunks.push(value);
    }
  } finally {
    try { reader.releaseLock(); } catch (_) {}
  }

  // Parse only the bounded bytes we have already consumed. Fetch-event requests do not
  // reliably expose the HTTP Content-Length that the browser may add later during network
  // serialization, so the stream itself is the authoritative pre-parser byte boundary.
  const bounded = new Blob(chunks, { type: contentType });
  return new Response(bounded, { headers: { "Content-Type": contentType } }).formData();
}

async function queueShareTarget(request) {
  if (!shareRequestIsTrustedNavigation(request)) throw new Error("share target provenance rejected");
  const contentType = String(request.headers.get("content-type") || "");
  if (!contentType.toLowerCase().startsWith("multipart/form-data;")) {
    throw new Error("share target requires multipart form data");
  }
  // Content-Length is an optional early rejection hint only. The actual body stream is
  // always byte-counted before multipart parsing, including real Android Web Share Target
  // requests where this network-generated forbidden header is not visible to JavaScript.
  rejectAdvertisedOversize(request);

  // A native share may be queued while the server is offline, but only a browser profile
  // that has already completed PR8 pairing may consume/persist the OS-provided multipart.
  const token = await self.CFStore.getToken();
  if (!token) throw new Error("share target requires a paired device");

  // Reject already-full queues before consuming the request body. Exact batch/count/byte
  // checks still happen atomically in CFStore at the persistence boundary after parsing.
  const usage = await self.CFStore.queueUsage();
  if (usage.entries >= LIMITS.maxQueueEntries || usage.fileBytes > LIMITS.maxQueueBytes) {
    throw new Error("share queue is full");
  }

  const data = await boundedMultipartFormData(request, contentType);
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

async function networkFirstConfig(request, event) {
  try {
    const response = await fetch(request, { cache: "no-store" });
    if (response.ok && response.type === "basic") {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)));
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw error;
  }
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

  // config.js reflects the live server's configured upload authority. When the server is
  // reachable, never let an older shell cache shadow a changed max_upload_bytes value;
  // while offline, fall back to the last successfully cached config so the PWA can still
  // open and preserve already-queued captures.
  if (url.pathname === new URL(CONFIG_URL).pathname) {
    event.respondWith(networkFirstConfig(request, event));
    return;
  }

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
