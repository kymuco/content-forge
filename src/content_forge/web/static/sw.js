importScripts("config.js", "shared.js");

"use strict";

const CACHE_PREFIX = `content-forge-shell:${self.registration.scope}:`;
// Retain known predecessor namespaces so installed shells may upgrade directly without
// leaving stale UI authority behind.
const OLDEST_LEGACY_CACHE_NAME = `${CACHE_PREFIX}v8`;
const LEGACY_CACHE_NAME = `${CACHE_PREFIX}v9`;
const PREVIOUS_CACHE_NAME = `${CACHE_PREFIX}v10`;
const LAST_CACHE_NAME = `${CACHE_PREFIX}v11`;
const EARLIER_CACHE_NAME = `${CACHE_PREFIX}v12`;
const OLDER_PREVIOUS_CACHE_NAME = `${CACHE_PREFIX}v13`;
const IMMEDIATE_PREVIOUS_CACHE_NAME = `${CACHE_PREFIX}v14`;
const PR29_CACHE_NAME = `${CACHE_PREFIX}v16`;
const PR31_CACHE_NAME = `${CACHE_PREFIX}v17`;
const PR32_CACHE_NAME = `${CACHE_PREFIX}v18`;
const CACHE_NAME = `${CACHE_PREFIX}v19`;
const LIMITS = self.CFStore.limits;
const ALLOWED_FIELDS = new Set(["title", "text", "url", "files"]);
const LIVE_LIMIT_NAMES = Object.freeze([
  "maxUploadBytes",
  "maxShareBodyBytes",
  "maxQueueBytes",
  "maxQueueEntries",
  "maxBatchEntries",
  "maxFilenameChars",
  "maxMimeChars",
  "maxUrlChars",
  "maxNoteChars",
]);

function appUrl(relative) {
  return new URL(relative, self.registration.scope).href;
}

const CONFIG_URL = appUrl("config.js");
const LIVE_CONFIG_URL = appUrl("config.json");
const SHELL_ASSETS = [
  appUrl("./"),
  CONFIG_URL,
  appUrl("styles.css"),
  appUrl("shared.js"),
  appUrl("app.js"),
  appUrl("review.js"),
  appUrl("production-home.js"),
  appUrl("dialogue.js"),
  appUrl("voice-cast.js"),
  appUrl("voiced-story.js"),
  appUrl("voiced-scene.js"),
  appUrl("production-profiles.js"),
  appUrl("production-library.js"),
  appUrl("publishing.js"),
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
          .filter((key) => (
            key === OLDEST_LEGACY_CACHE_NAME
            || key === LEGACY_CACHE_NAME
            || key === PREVIOUS_CACHE_NAME
            || key === LAST_CACHE_NAME
            || key === EARLIER_CACHE_NAME
            || key === OLDER_PREVIOUS_CACHE_NAME
            || key === IMMEDIATE_PREVIOUS_CACHE_NAME
            || key === PR29_CACHE_NAME
            || key === PR31_CACHE_NAME
            || key === PR32_CACHE_NAME
            || (key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          ))
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
  // FetchEvent requests may not expose the browser-generated HTTP Content-Length. Parse it
  // only as an optional hint here; the live server authority is applied after refresh.
  const raw = request.headers.get("content-length");
  if (raw == null) return null;
  if (!/^\d+$/.test(raw)) throw new Error("invalid shared Content-Length");
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error("invalid shared Content-Length");
  }
  return value;
}

function validateLiveLimits(payload) {
  if (!payload || typeof payload !== "object") throw new Error("invalid live PWA limits");
  const normalized = {};
  for (const name of LIVE_LIMIT_NAMES) {
    const value = Number(payload[name]);
    if (!Number.isSafeInteger(value) || value < 1) {
      throw new Error(`invalid live PWA limit: ${name}`);
    }
    normalized[name] = value;
  }
  if (normalized.maxShareBodyBytes < normalized.maxUploadBytes) {
    throw new Error("invalid live PWA upload limits");
  }
  return Object.freeze(normalized);
}

async function currentShareLimits() {
  let response;
  try {
    response = await fetch(LIVE_CONFIG_URL, { cache: "no-store" });
  } catch (_) {
    // Native shares must remain capturable while the local server is actually offline.
    // A failure before response headers are received is a genuine network fallback.
    return LIMITS;
  }
  if (!response.ok) throw new Error(`live PWA limits unavailable (${response.status})`);

  let body;
  try {
    // Reading the response body is still network I/O. If the connection dies after the
    // headers arrive, preserve offline capture by falling back to the frozen authority.
    body = await response.text();
  } catch (_) {
    return LIMITS;
  }

  let payload;
  try {
    // Once the complete body was received, malformed JSON/configuration is not an
    // offline condition. Treat it as invalid live authority and fail closed.
    payload = JSON.parse(body);
  } catch (_) {
    throw new Error("invalid live PWA limits");
  }
  return validateLiveLimits(payload);
}

async function boundedMultipartFormData(request, contentType, limits) {
  if (!request.body) throw new Error("share target has no request body");
  const reader = request.body.getReader();
  let totalBytes = 0;
  let finished = false;

  // Stream directly into the browser's multipart parser instead of first retaining every
  // chunk and constructing a second full-size Blob. The wrapper never enqueues a chunk
  // that would cross the active cap, so parser input is bounded while peak JS memory
  // remains proportional to stream buffering rather than the complete shared payload.
  const boundedStream = new ReadableStream({
    async pull(controller) {
      if (finished) return;
      try {
        const { done, value } = await reader.read();
        if (done) {
          finished = true;
          controller.close();
          return;
        }
        if (!(value instanceof Uint8Array)) {
          throw new Error("invalid share target body chunk");
        }
        totalBytes += value.byteLength;
        if (totalBytes > limits.maxShareBodyBytes) {
          finished = true;
          try { await reader.cancel("share target body limit exceeded"); } catch (_) {}
          controller.error(new Error("shared payload exceeds the local queue limit"));
          return;
        }
        controller.enqueue(value);
      } catch (error) {
        finished = true;
        try { await reader.cancel(error); } catch (_) {}
        controller.error(error);
      }
    },
    async cancel(reason) {
      finished = true;
      try { await reader.cancel(reason); } catch (_) {}
    },
  });

  try {
    return await new Response(boundedStream, {
      headers: { "Content-Type": contentType },
    }).formData();
  } finally {
    try { reader.releaseLock(); } catch (_) {}
  }
}

async function queueShareTarget(request) {
  if (!shareRequestIsTrustedNavigation(request)) throw new Error("share target provenance rejected");
  const contentType = String(request.headers.get("content-type") || "");
  if (!contentType.toLowerCase().startsWith("multipart/form-data;")) {
    throw new Error("share target requires multipart form data");
  }
  const contentLength = boundedContentLength(request);

  // A native share may be queued while the server is offline, but only a browser profile
  // that has already completed PR8 pairing may consume/persist the OS-provided multipart.
  const token = await self.CFStore.getToken();
  if (!token) throw new Error("share target requires a paired device");

  // If the server is reachable, refresh authority before consuming this share. A stale
  // active worker therefore cannot admit bytes using yesterday's max_upload_bytes. Only
  // a genuine fetch/body-stream network failure falls back to frozen offline limits.
  const activeLimits = await currentShareLimits();
  if (contentLength != null && contentLength > activeLimits.maxShareBodyBytes) {
    throw new Error("shared payload exceeds the local queue limit");
  }

  // Reject already-full queues before consuming the request body. Exact checks still
  // happen at the shared IndexedDB persistence boundary after parsing.
  const usage = await self.CFStore.queueUsage();
  if (usage.entries >= activeLimits.maxQueueEntries || usage.fileBytes > activeLimits.maxQueueBytes) {
    throw new Error("share queue is full");
  }

  // Never call request.formData() on the unbounded FetchEvent request. The parser consumes
  // only the stream wrapper capped by the freshly resolved authority above.
  const data = await boundedMultipartFormData(request, contentType, activeLimits);
  for (const key of data.keys()) {
    if (!ALLOWED_FIELDS.has(key)) throw new Error("share target contains an unsupported field");
  }

  const title = normalizeString(data.get("title"));
  const text = normalizeString(data.get("text"));
  const url = normalizeString(data.get("url"));
  const rawFiles = data.getAll("files");
  if (rawFiles.length > activeLimits.maxBatchEntries) throw new Error("too many shared files");
  if (rawFiles.some((item) => !(item instanceof File))) throw new Error("invalid shared file field");
  const note = [title, text].filter(Boolean).join("\n");

  if (url.length > activeLimits.maxUrlChars) throw new Error("shared URL is too long");
  if (note.length > activeLimits.maxNoteChars) throw new Error("shared note is too long");
  if (!rawFiles.length && !url && !note) throw new Error("empty share target payload");

  const normalizedFiles = rawFiles.map((file) => {
    if (file.size > activeLimits.maxUploadBytes) throw new Error("shared file exceeds upload limit");
    const originalName = file.name || "shared-file";
    const mimeType = file.type || "application/octet-stream";
    if (originalName.length > activeLimits.maxFilenameChars) throw new Error("shared filename is too long");
    if (mimeType.length > activeLimits.maxMimeChars) throw new Error("shared MIME type is too long");
    return { file, originalName, mimeType };
  });

  const records = normalizedFiles.length
    ? normalizedFiles.map(({ file, originalName, mimeType }) => ({
        kind: "file",
        file,
        originalName,
        mimeType,
        sourceUrl: url || null,
        note: note || null,
      }))
    : [{
        kind: "url_note",
        sourceUrl: url || null,
        note: note || null,
      }];

  const incomingFileBytes = normalizedFiles.reduce((total, item) => total + item.file.size, 0);
  if (usage.entries + records.length > activeLimits.maxQueueEntries) throw new Error("share queue is full");
  if (usage.fileBytes + incomingFileBytes > activeLimits.maxQueueBytes) {
    throw new Error("share queue byte limit exceeded");
  }

  // Keep the offline path on the worker's frozen authority, preserving the established
  // shared-store contract. When the reachable server supplied a fresh snapshot, pass that
  // exact validated authority into the same atomic IndexedDB transaction so an increased
  // limit cannot be vetoed by the old worker's imported config after all live checks pass.
  if (activeLimits === LIMITS) {
    await self.CFStore.enqueueShares(records);
  } else {
    await self.CFStore.enqueueSharesWithLimits(records, activeLimits);
  }
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
