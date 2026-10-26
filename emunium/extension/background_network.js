// background_network.js - tab-scoped network response buffer and waiters.
// Contract: docs/architecture/bridge-protocol.md section 6 (waitForResponse, getRecentResponses).
(() => {
  "use strict";

  const scope = (globalThis.EmuniumBackground = globalThis.EmuniumBackground || {});

  const BUFFER_MAX_ENTRIES = 500;
  const BUFFER_MAX_AGE_MS = 120000;

  const networkState = {
    // Each slot is {entry, receivedAt}; receivedAt is the worker clock at record time.
    recentResponses: [],
    pendingWaiters: [],
  };

  function invalidArgument(message) {
    if (typeof scope.codedError === "function") {
      return scope.codedError("invalid_argument", message);
    }
    const error = new Error(message);
    error.code = "invalid_argument";
    return error;
  }

  // Only `*` is a wildcard; every other character (including `?`) matches literally.
  function compilePattern(pattern) {
    if (typeof pattern !== "string" || pattern.length === 0) {
      throw invalidArgument("'pattern' must be a non-empty glob string");
    }
    const body = pattern
      .split("*")
      .map((part) => part.replace(/[.*+?^${}()|[\]\\\/-]/g, "\\$&"))
      .join("[\\s\\S]*");
    return new RegExp("^" + body + "$");
  }

  function makeFilter({ pattern, tabId, since, anyTab }) {
    const regex = compilePattern(pattern);
    const scoped = anyTab !== true;
    const wantedTab = Number.isInteger(tabId) ? tabId : null;
    const sinceMs = typeof since === "number" && Number.isFinite(since) ? since : null;
    return (entry) => {
      if (scoped) {
        if (!Number.isInteger(entry.tabId) || entry.tabId < 0) {
          return false;
        }
        if (wantedTab !== null && entry.tabId !== wantedTab) {
          return false;
        }
      }
      if (sinceMs !== null && !(entry.timeStamp >= sinceMs)) {
        return false;
      }
      return regex.test(entry.url);
    };
  }

  function prune(now) {
    const slots = networkState.recentResponses;
    let drop = 0;
    while (drop < slots.length && now - slots[drop].receivedAt > BUFFER_MAX_AGE_MS) {
      drop += 1;
    }
    const overflow = slots.length - drop - BUFFER_MAX_ENTRIES;
    if (overflow > 0) {
      drop += overflow;
    }
    if (drop > 0) {
      slots.splice(0, drop);
    }
  }

  function makeEntry(details, failure) {
    const entry = {
      url: typeof details.url === "string" ? details.url : "",
      statusCode: failure ? 0 : typeof details.statusCode === "number" ? details.statusCode : 0,
      method: details.method || "GET",
      type: details.type,
      timeStamp: typeof details.timeStamp === "number" && Number.isFinite(details.timeStamp) ? details.timeStamp : Date.now(),
      tabId: Number.isInteger(details.tabId) ? details.tabId : -1,
      requestId: details.requestId === undefined || details.requestId === null ? null : String(details.requestId),
    };
    if (failure) {
      entry.error = typeof details.error === "string" && details.error ? details.error : "net::ERR_FAILED";
    }
    return entry;
  }

  function record(details, failure) {
    if (!details || typeof details !== "object") {
      return;
    }
    const entry = makeEntry(details, failure);
    const now = Date.now();
    networkState.recentResponses.push({ entry, receivedAt: now });
    prune(now);

    const remaining = [];
    for (const waiter of networkState.pendingWaiters) {
      if (waiter.matches(entry)) {
        clearTimeout(waiter.timer);
        waiter.resolve({ ...entry });
      } else {
        remaining.push(waiter);
      }
    }
    networkState.pendingWaiters = remaining;
  }

  function addResponse(details) {
    record(details, false);
  }

  function addError(details) {
    record(details, true);
  }

  function waitForResponse({ pattern, timeoutMs, tabId = null, since = null, anyTab = false } = {}) {
    let matches;
    try {
      matches = makeFilter({ pattern, tabId, since, anyTab });
    } catch (error) {
      return Promise.reject(error);
    }
    if (typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs) || timeoutMs < 0) {
      return Promise.reject(invalidArgument("'timeoutMs' must be a non-negative number"));
    }

    prune(Date.now());
    const buffered = networkState.recentResponses.find((slot) => matches(slot.entry));
    if (buffered) {
      return Promise.resolve({ ...buffered.entry });
    }

    return new Promise((resolve) => {
      const waiter = { matches, resolve, timer: null };
      waiter.timer = setTimeout(() => {
        const index = networkState.pendingWaiters.indexOf(waiter);
        if (index !== -1) {
          networkState.pendingWaiters.splice(index, 1);
        }
        resolve(null);
      }, timeoutMs);
      networkState.pendingWaiters.push(waiter);
    });
  }

  function getRecentResponses({ since = null, tabId = null, anyTab = false } = {}) {
    const matches = makeFilter({ pattern: "*", tabId, since, anyTab });
    prune(Date.now());
    return networkState.recentResponses.filter((slot) => matches(slot.entry)).map((slot) => ({ ...slot.entry }));
  }

  const webRequest = typeof chrome !== "undefined" && chrome && chrome.webRequest ? chrome.webRequest : null;
  if (webRequest) {
    const filter = { urls: ["<all_urls>"] };
    if (webRequest.onCompleted && typeof webRequest.onCompleted.addListener === "function") {
      webRequest.onCompleted.addListener(addResponse, filter);
    }
    if (webRequest.onErrorOccurred && typeof webRequest.onErrorOccurred.addListener === "function") {
      webRequest.onErrorOccurred.addListener(addError, filter);
    }
  }

  Object.assign(scope, {
    networkState,
    waitForResponse,
    getRecentResponses,
  });
})();
