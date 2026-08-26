importScripts("shared.js");

"use strict";

const CACHE_PREFIX = `content-forge-shell:${self.registration.scope}:`;
const CACHE_NAME = `${CACHE_PREFIX}v2`;
const MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;
const MULTIPART_OVERHEAD_BUDGET = 1024 * 1024;
const MAX_SHARE_BODY_BYTES = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BUDGET;
const MAX_QUEUE_BYTES = MAX_UPLOAD_BYTES;
const MAX_QUEUE_ENTRIES = 256;
const MAX_SHARED_FILES = 16;
const MAX_FILENAME_CHARS = 1024;
const MAX_MIME_CHARS = 255;
const MAX_URL_CHARS = 4096;
const MAX_NOTE_CHARS = 8192;
const ALLOWED_FIELDS = new Set(["title", "text", "url", "files"]);

function appUrl(relative) {
  return new URL(relative, self.registration.scope).href;
}

const SHELL_ASSETS = [
  appUrl("./"),
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
  if (!Number.isSafeInteger(value) || value < 0 || value > MAX_SHARE_BODY_BYTES) {
    throw new Error("shared payload exceeds the local queue limit");
  }
  return value;
}

function queuedFileBytes(records) {
  return records.reduce((total, record) => {
    if (record && record.kind === "file" && record.file instanceof Blob) return total + record.file.size;
    return total;
  }, 0);
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

  const existing = await self.CFStore.listShares();
  if (existing.length >= MAX_QUEUE_ENTRIES) throw new Error("share queue is full");
  const existingBytes = queuedFileBytes(existing);
  if (existingBytes > MAX_QUEUE_BYTES) throw new Error("share queue byte limit exceeded");

  const data = await request.formData();
  for (const key of data.keys()) {
    if (!ALLOWED_FIELDS.has(key)) throw new Error("share target contains an unsupported field");
  }

  const title = normalizeString(data.get("title"));
  const text = normalizeString(data.get("text"));
  const url = normalizeString(data.get("url"));
  const rawFiles = data.getAll("files");
  if (rawFiles.length > MAX_SHARED_FILES) throw new Error("too many shared files");
  if (rawFiles.some((item) => !(item instanceof File))) throw new Error("invalid shared file field");
  const files = rawFiles;
  const note = [title, text].filter(Boolean).join("\n");

  if (url.length > MAX_URL_CHARS) throw new Error("shared URL is too long");
  if (note.length > MAX_NOTE_CHARS) throw new Error("shared note is too long");
  if (!files.length && !url && !note) throw new Error("empty share target payload");
  if (existing.length + Math.max(files.length, 1) > MAX_QUEUE_ENTRIES) throw new Error("share queue is full");

  let incomingBytes = 0;
  for (const file of files) {
    const name = file.name || "shared-file";
    const mimeType = file.type || "application/octet-stream";
    if (name.length > MAX_FILENAME_CHARS) throw new Error("shared filename is too long");
    if (mimeType.length > MAX_MIME_CHARS) throw new Error("shared MIME type is too long");
    if (file.size < 0 || file.size > MAX_UPLOAD_BYTES) throw new Error("shared file exceeds upload limit");
    incomingBytes += file.size;
    if (incomingBytes > MAX_QUEUE_BYTES || existingBytes + incomingBytes > MAX_QUEUE_BYTES) {
      throw new Error("share queue byte limit exceeded");
    }
  }

  if (files.length) {
    for (const file of files) {
      await self.CFStore.enqueueShare({
        kind: "file",
        file,
        originalName: file.name || "shared-file",
        mimeType: file.type || "application/octet-stream",
        sourceUrl: url || null,
        note: note || null,
      });
    }
  } else {
    await self.CFStore.enqueueShare({
      kind: "url_note",
      sourceUrl: url || null,
      note: note || null,
    });
  }

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