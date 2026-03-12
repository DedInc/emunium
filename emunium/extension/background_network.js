(() => {
  "use strict";

  const scope = globalThis.EmuniumBackground;
  const BUFFER_SIZE = 50;

  const networkState = {
    recentResponses: [],
    pendingWaiters: [],
  };

  function matchesPattern(url, pattern) {
    const regex = new RegExp(
      "^" +
        pattern
          .replace(/[.+^${}()|[\]\\]/g, "\\$&")
          .replace(/\*/g, ".*") +
        "$"
    );
    return regex.test(url);
  }

  function addResponse(details) {
    const entry = {
      url: details.url,
      statusCode: details.statusCode,
      method: details.method || "GET",
      type: details.type,
      timeStamp: details.timeStamp,
      tabId: details.tabId,
    };

    networkState.recentResponses.push(entry);
    if (networkState.recentResponses.length > BUFFER_SIZE) {
      networkState.recentResponses.shift();
    }

    const remaining = [];
    for (const waiter of networkState.pendingWaiters) {
      if (matchesPattern(entry.url, waiter.pattern)) {
        waiter.resolve(entry);
      } else {
        remaining.push(waiter);
      }
    }
    networkState.pendingWaiters = remaining;
  }

  function waitForResponse(pattern, timeoutMs) {
    const existing = networkState.recentResponses.find((r) =>
      matchesPattern(r.url, pattern)
    );
    if (existing) {
      return Promise.resolve(existing);
    }

    return new Promise((resolve) => {
      const waiter = { pattern, resolve };
      networkState.pendingWaiters.push(waiter);

      setTimeout(() => {
        const idx = networkState.pendingWaiters.indexOf(waiter);
        if (idx !== -1) {
          networkState.pendingWaiters.splice(idx, 1);
          resolve(null);
        }
      }, timeoutMs);
    });
  }

  function getRecentResponses() {
    return networkState.recentResponses.slice();
  }

  if (typeof chrome !== "undefined" && chrome.webRequest) {
    chrome.webRequest.onCompleted.addListener(
      (details) => addResponse(details),
      { urls: ["<all_urls>"] }
    );
  }

  Object.assign(scope, {
    networkState,
    waitForResponse,
    getRecentResponses,
  });
})();
