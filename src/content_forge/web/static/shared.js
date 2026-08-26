(function (root) {
  "use strict";

  const scopeUrl = new URL("./", root.location.href);
  const DB_NAME = `content-forge-pwa-v2:${encodeURIComponent(scopeUrl.pathname)}`;
  const DB_VERSION = 1;
  const KV_STORE = "kv";
  const SHARE_STORE = "shares";
  const TOKEN_KEY = "bearer-token";

  const config = root.CF_CONFIG;
  if (!config || typeof config !== "object") {
    throw new Error("Content Forge PWA configuration is missing");
  }

  function positiveInteger(name) {
    const value = Number(config[name]);
    if (!Number.isSafeInteger(value) || value < 1) {
      throw new Error(`Invalid Content Forge PWA configuration: ${name}`);
    }
    return value;
  }

  const LIMITS = Object.freeze({
    maxUploadBytes: positiveInteger("maxUploadBytes"),
    maxShareBodyBytes: positiveInteger("maxShareBodyBytes"),
    maxQueueBytes: positiveInteger("maxQueueBytes"),
    maxQueueEntries: positiveInteger("maxQueueEntries"),
    maxBatchEntries: positiveInteger("maxBatchEntries"),
    maxFilenameChars: positiveInteger("maxFilenameChars"),
    maxMimeChars: positiveInteger("maxMimeChars"),
    maxUrlChars: positiveInteger("maxUrlChars"),
    maxNoteChars: positiveInteger("maxNoteChars"),
  });

  function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(KV_STORE)) db.createObjectStore(KV_STORE);
        if (!db.objectStoreNames.contains(SHARE_STORE)) {
          const shares = db.createObjectStore(SHARE_STORE, { keyPath: "id" });
          shares.createIndex("createdAt", "createdAt", { unique: false });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("IndexedDB open failed"));
    });
  }

  function requestResult(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("IndexedDB request failed"));
    });
  }

  function transactionDone(transaction) {
    return new Promise((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(transaction.error || new Error("IndexedDB transaction aborted"));
      transaction.onerror = () => reject(transaction.error || new Error("IndexedDB transaction failed"));
    });
  }

  function optionalString(value, maxChars, label) {
    if (value == null || value === "") return null;
    if (typeof value !== "string") throw new Error(`${label} must be text`);
    if (value.length > maxChars) throw new Error(`${label} is too long`);
    return value;
  }

  function normalizeShare(record) {
    if (!record || typeof record !== "object") throw new Error("invalid share queue record");
    const id = record.id || crypto.randomUUID();
    const createdAt = record.createdAt || new Date().toISOString();
    if (typeof id !== "string" || !id) throw new Error("share queue record requires an ID");
    if (typeof createdAt !== "string" || !createdAt) throw new Error("share queue record requires createdAt");

    const sourceUrl = optionalString(record.sourceUrl, LIMITS.maxUrlChars, "shared URL");
    const note = optionalString(record.note, LIMITS.maxNoteChars, "shared note");

    if (record.kind === "file") {
      if (!(record.file instanceof Blob)) throw new Error("file queue record requires Blob bytes");
      if (record.file.size > LIMITS.maxUploadBytes) throw new Error("shared file exceeds upload limit");
      const originalName = record.originalName || record.file.name || "shared-file";
      const mimeType = record.mimeType || record.file.type || "application/octet-stream";
      if (typeof originalName !== "string" || originalName.length > LIMITS.maxFilenameChars) {
        throw new Error("shared filename is too long");
      }
      if (typeof mimeType !== "string" || mimeType.length > LIMITS.maxMimeChars) {
        throw new Error("shared MIME type is too long");
      }
      return {
        kind: "file",
        id,
        createdAt,
        file: record.file,
        originalName,
        mimeType,
        sourceUrl,
        note,
      };
    }

    if (record.kind === "url_note") {
      if (!sourceUrl && !note) throw new Error("URL/note queue record is empty");
      return { kind: "url_note", id, createdAt, sourceUrl, note };
    }

    throw new Error("unsupported share queue record kind");
  }

  function queuedFileBytes(records) {
    return records.reduce((total, record) => {
      if (record && record.kind === "file" && record.file instanceof Blob) {
        return total + record.file.size;
      }
      return total;
    }, 0);
  }

  function validateQueueMutation(existing, incoming) {
    if (incoming.length < 1) throw new Error("share queue batch is empty");
    if (incoming.length > LIMITS.maxBatchEntries) throw new Error("too many items in one capture");
    if (existing.length + incoming.length > LIMITS.maxQueueEntries) throw new Error("share queue is full");

    const existingBytes = queuedFileBytes(existing);
    const incomingBytes = queuedFileBytes(incoming);
    if (existingBytes > LIMITS.maxQueueBytes || incomingBytes > LIMITS.maxQueueBytes) {
      throw new Error("share queue byte limit exceeded");
    }
    if (existingBytes + incomingBytes > LIMITS.maxQueueBytes) {
      throw new Error("share queue byte limit exceeded");
    }
  }

  async function getToken() {
    const db = await openDatabase();
    try {
      const tx = db.transaction(KV_STORE, "readonly");
      const value = await requestResult(tx.objectStore(KV_STORE).get(TOKEN_KEY));
      await transactionDone(tx);
      return typeof value === "string" && value ? value : null;
    } finally { db.close(); }
  }

  async function setToken(token) {
    if (typeof token !== "string" || !token) throw new Error("bearer token is required");
    const db = await openDatabase();
    try {
      const tx = db.transaction(KV_STORE, "readwrite");
      tx.objectStore(KV_STORE).put(token, TOKEN_KEY);
      await transactionDone(tx);
    } finally { db.close(); }
  }

  async function clearToken() {
    const db = await openDatabase();
    try {
      const tx = db.transaction(KV_STORE, "readwrite");
      tx.objectStore(KV_STORE).delete(TOKEN_KEY);
      await transactionDone(tx);
    } finally { db.close(); }
  }

  async function enqueueShares(records) {
    if (!Array.isArray(records)) throw new Error("share queue batch must be an array");
    const entries = records.map(normalizeShare);
    if (!entries.length) throw new Error("share queue batch is empty");

    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      let cause = null;
      let tx;
      try {
        tx = db.transaction(SHARE_STORE, "readwrite");
      } catch (error) {
        db.close();
        reject(error);
        return;
      }
      const store = tx.objectStore(SHARE_STORE);
      const read = store.getAll();

      read.onerror = () => {
        cause = read.error || new Error("IndexedDB queue read failed");
        try { tx.abort(); } catch (_) {}
      };
      read.onsuccess = () => {
        try {
          validateQueueMutation(Array.isArray(read.result) ? read.result : [], entries);
          for (const entry of entries) store.add(entry);
        } catch (error) {
          cause = error;
          try { tx.abort(); } catch (_) {}
        }
      };
      tx.oncomplete = () => {
        db.close();
        resolve(entries);
      };
      tx.onabort = () => {
        const error = cause || tx.error || new Error("IndexedDB queue transaction aborted");
        db.close();
        reject(error);
      };
      tx.onerror = () => {
        if (!cause) cause = tx.error || new Error("IndexedDB queue transaction failed");
      };
    });
  }

  async function enqueueShare(record) {
    const entries = await enqueueShares([record]);
    return entries[0];
  }

  async function listShares() {
    const db = await openDatabase();
    try {
      const tx = db.transaction(SHARE_STORE, "readonly");
      const values = await requestResult(tx.objectStore(SHARE_STORE).getAll());
      await transactionDone(tx);
      return values.sort((left, right) => String(left.createdAt).localeCompare(String(right.createdAt)));
    } finally { db.close(); }
  }

  async function queueUsage() {
    const records = await listShares();
    return Object.freeze({ entries: records.length, fileBytes: queuedFileBytes(records) });
  }

  async function deleteShare(id) {
    const db = await openDatabase();
    try {
      const tx = db.transaction(SHARE_STORE, "readwrite");
      tx.objectStore(SHARE_STORE).delete(id);
      await transactionDone(tx);
    } finally { db.close(); }
  }

  root.CFStore = Object.freeze({
    scope: scopeUrl.href,
    limits: LIMITS,
    getToken,
    setToken,
    clearToken,
    enqueueShare,
    enqueueShares,
    listShares,
    queueUsage,
    deleteShare,
  });
})(typeof window !== "undefined" ? window : self);
