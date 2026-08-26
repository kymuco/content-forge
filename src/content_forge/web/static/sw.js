importScripts("shared.js");

"use strict";

const CACHE_NAME = "content-forge-shell-v1";

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
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function normalizeString(value) {
  return typeof value === "string" ? value.trim() : "";
}

async function queueShareTarget(request) {
  const data = await request.formData();
  const title = normalizeString(data.get("title"));
  const text = normalizeString(data.get("text"));
  const url = normalizeString(data.get("url"));
  const files = data.getAll("files").filter((item) => item instanceof File && item.size >= 0);

  if (files.length) {
    for (const file of files) {
      await self.CFStore.enqueueShare({
        kind: "file",
        file,
        originalName: file.name || "shared-file",
        mimeType: file.type || "application/octet-stream",
        sourceUrl: url || null,
        note: [title, text].filter(Boolean).join("\n") || null,
      });
    }
  } else if (url || text || title) {
    await self.CFStore.enqueueShare({
      kind: "url_note",
      sourceUrl: url || null,
      note: [title, text].filter(Boolean).join("\n") || null,
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
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    })
  );
});
