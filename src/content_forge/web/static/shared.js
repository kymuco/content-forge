(function (root) {
  "use strict";

  const DB_NAME = "content-forge-pwa-v1";
  const DB_VERSION = 1;
  const KV_STORE = "kv";
  const SHARE_STORE = "shares";
  const TOKEN_KEY = "bearer-token";

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

  async function enqueueShare(record) {
    const entry = Object.assign({}, record, {
      id: record.id || crypto.randomUUID(),
      createdAt: record.createdAt || new Date().toISOString(),
    });
    const db = await openDatabase();
    try {
      const tx = db.transaction(SHARE_STORE, "readwrite");
      tx.objectStore(SHARE_STORE).put(entry);
      await transactionDone(tx);
      return entry;
    } finally { db.close(); }
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

  async function deleteShare(id) {
    const db = await openDatabase();
    try {
      const tx = db.transaction(SHARE_STORE, "readwrite");
      tx.objectStore(SHARE_STORE).delete(id);
      await transactionDone(tx);
    } finally { db.close(); }
  }

  root.CFStore = Object.freeze({ getToken, setToken, clearToken, enqueueShare, listShares, deleteShare });
})(typeof window !== "undefined" ? window : self);
