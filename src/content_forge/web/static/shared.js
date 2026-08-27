(function (root) {
  "use strict";

  const scopeUrl = new URL("./", root.location.href);
  const DB_NAME = `content-forge-pwa-v2:${encodeURIComponent(scopeUrl.pathname)}`;
  const DB_VERSION = 1;
  const KV_STORE = "kv";
  const SHARE_STORE = "shares";
  const TOKEN_KEY = "bearer-token";
  const LIMIT_NAMES = Object.freeze([
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

  const config = root.CF_CONFIG;
  if (!config || typeof config !== "object") {
    throw new Error("Content Forge PWA configuration is missing");
  }

  function positiveInteger(name, source) {
    const value = Number(source[name]);
    if (!Number.isSafeInteger(value) || value < 1) {
      throw new Error(`Invalid Content Forge PWA configuration: ${name}`);
    }
    return value;
  }

  function normalizeLimits(source) {
    if (!source || typeof source !== "object") {
      throw new Error("Content Forge PWA queue authority is missing");
    }
    const normalized = {};
    for (const name of LIMIT_NAMES) normalized[name] = positiveInteger(name, source);
    if (normalized.maxShareBodyBytes < normalized.maxUploadBytes) {
      throw new Error("Invalid Content Forge PWA upload limits");
    }
    return Object.freeze(normalized);
  }

  const LIMITS = normalizeLimits(config);

  function queueAuthority(authority) {
    if (authority == null || authority === LIMITS) return LIMITS;
    return normalizeLimits(authority);
  }

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

  function normalizeShare(record, limits) {
    if (!record || typeof record !== "object") throw new Error("invalid share queue record");
    const id = record.id || crypto.randomUUID();
    const createdAt = record.createdAt || new Date().toISOString();
    if (typeof id !== "string" || !id) throw new Error("share queue record requires an ID");
    if (typeof createdAt !== "string" || !createdAt) throw new Error("share queue record requires createdAt");

    const sourceUrl = optionalString(record.sourceUrl, limits.maxUrlChars, "shared URL");
    const note = optionalString(record.note, limits.maxNoteChars, "shared note");

    if (record.kind === "file") {
      if (!(record.file instanceof Blob)) throw new Error("file queue record requires Blob bytes");
      if (record.file.size > limits.maxUploadBytes) throw new Error("shared file exceeds upload limit");
      const originalName = record.originalName || record.file.name || "shared-file";
      const mimeType = record.mimeType || record.file.type || "application/octet-stream";
      if (typeof originalName !== "string" || originalName.length > limits.maxFilenameChars) {
        throw new Error("shared filename is too long");
      }
      if (typeof mimeType !== "string" || mimeType.length > limits.maxMimeChars) {
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

  function validateQueueMutation(existing, incoming, limits) {
    if (incoming.length < 1) throw new Error("share queue batch is empty");
    if (incoming.length > limits.maxBatchEntries) throw new Error("too many items in one capture");
    if (existing.length + incoming.length > limits.maxQueueEntries) throw new Error("share queue is full");

    const existingBytes = queuedFileBytes(existing);
    const incomingBytes = queuedFileBytes(incoming);
    if (existingBytes > limits.maxQueueBytes || incomingBytes > limits.maxQueueBytes) {
      throw new Error("share queue byte limit exceeded");
    }
    if (existingBytes + incomingBytes > limits.maxQueueBytes) {
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
    return new Promise((resolve, reject) => {
      let cause = null;
      let tx;
      try {
        // IndexedDB serializes overlapping read/write transactions for this object store.
        // The read and conditional put therefore form one cross-tab compare-and-set: the
        // first issued bearer claims the empty slot, while a concurrent different bearer
        // cannot replace it and is returned to exchangePairing's revocation path.
        tx = db.transaction(KV_STORE, "readwrite");
      } catch (error) {
        db.close();
        reject(error);
        return;
      }
      const store = tx.objectStore(KV_STORE);
      const read = store.get(TOKEN_KEY);
      read.onerror = () => {
        cause = read.error || new Error("IndexedDB token read failed");
        try { tx.abort(); } catch (_) {}
      };
      read.onsuccess = () => {
        const existing = read.result;
        if (existing !== undefined) {
          if (existing === token) return;
          cause = new Error("pairing token slot is already occupied");
          try { tx.abort(); } catch (_) {}
          return;
        }
        const write = store.put(token, TOKEN_KEY);
        write.onerror = () => {
          cause = write.error || new Error("IndexedDB token write failed");
          try { tx.abort(); } catch (_) {}
        };
      };
      tx.oncomplete = () => {
        db.close();
        resolve();
      };
      tx.onabort = () => {
        const error = cause || tx.error || new Error("IndexedDB token claim transaction aborted");
        db.close();
        reject(error);
      };
      tx.onerror = () => {
        if (!cause) cause = tx.error || new Error("IndexedDB token claim transaction failed");
      };
    });
  }

  async function clearToken() {
    const db = await openDatabase();
    try {
      const tx = db.transaction(KV_STORE, "readwrite");
      tx.objectStore(KV_STORE).delete(TOKEN_KEY);
      await transactionDone(tx);
    } finally { db.close(); }
  }

  async function clearTokenIfMatches(expectedToken) {
    if (typeof expectedToken !== "string" || !expectedToken) {
      throw new Error("expected bearer token is required");
    }
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      let matched = false;
      let cause = null;
      let tx;
      try {
        tx = db.transaction(KV_STORE, "readwrite");
      } catch (error) {
        db.close();
        reject(error);
        return;
      }
      const store = tx.objectStore(KV_STORE);
      const read = store.get(TOKEN_KEY);
      read.onerror = () => {
        cause = read.error || new Error("IndexedDB token read failed");
        try { tx.abort(); } catch (_) {}
      };
      read.onsuccess = () => {
        if (read.result !== expectedToken) return;
        matched = true;
        const deletion = store.delete(TOKEN_KEY);
        deletion.onerror = () => {
          cause = deletion.error || new Error("IndexedDB token deletion failed");
          try { tx.abort(); } catch (_) {}
        };
      };
      tx.oncomplete = () => {
        db.close();
        resolve(matched);
      };
      tx.onabort = () => {
        const error = cause || tx.error || new Error("IndexedDB token cleanup transaction aborted");
        db.close();
        reject(error);
      };
      tx.onerror = () => {
        if (!cause) cause = tx.error || new Error("IndexedDB token cleanup transaction failed");
      };
    });
  }

  async function enqueueSharesWithLimits(records, authority) {
    if (!Array.isArray(records)) throw new Error("share queue batch must be an array");
    const limits = queueAuthority(authority);
    const entries = records.map((record) => normalizeShare(record, limits));
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
          validateQueueMutation(Array.isArray(read.result) ? read.result : [], entries, limits);
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

  async function enqueueShares(records) {
    return enqueueSharesWithLimits(records, LIMITS);
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
    clearTokenIfMatches,
    enqueueShare,
    enqueueShares,
    enqueueSharesWithLimits,
    listShares,
    queueUsage,
    deleteShare,
  });
})(typeof window !== "undefined" ? window : self);
