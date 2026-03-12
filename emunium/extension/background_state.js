(() => {
  "use strict";

  const scope = (globalThis.EmuniumBackground = globalThis.EmuniumBackground || {});
  const state = scope.state || {
    ws: null,
    wsUrl: null,
    reconnectTimer: null,
    readyTabs: new Set(),
    tabDocIds: new Map(),
    pinnedTabId: null,
  };

  async function getPort() {
    try {
      const response = await fetch(
        chrome.runtime.getURL("port.json") + "?t=" + Date.now(),
        { cache: "no-store" }
      );
      if (response.ok) {
        const data = await response.json();
        if (data.port) {
          return data.port;
        }
      }
    } catch {}
    return null;
  }

  function clearReconnect() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  }

  function clearKeepAlive(socket) {
    if (socket && socket._keepAlive) {
      clearInterval(socket._keepAlive);
      socket._keepAlive = null;
    }
  }

  function startKeepAlive(socket) {
    clearKeepAlive(socket);
    socket._keepAlive = setInterval(() => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ event: "ping" }));
      }
    }, 20000);
  }

  function clearTrackedTab(tabId) {
    state.readyTabs.delete(tabId);
    state.tabDocIds.delete(tabId);
  }

  function clearPinnedTab(tabId) {
    if (state.pinnedTabId === tabId) {
      state.pinnedTabId = null;
    }
  }

  function scheduleReconnect(delay) {
    if (state.reconnectTimer) {
      return;
    }
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      connect();
    }, delay);
  }

  async function connect() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      return;
    }
    const port = await getPort();
    if (!port) {
      scheduleReconnect(2000);
      return;
    }

    state.wsUrl = `ws://127.0.0.1:${port}`;
    try {
      state.ws = new WebSocket(state.wsUrl);
    } catch {
      scheduleReconnect(2000);
      return;
    }

    if (typeof scope.attachSocketHandlers === "function") {
      scope.attachSocketHandlers(state.ws);
    }
  }

  function send(data) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(data));
    }
  }

  async function getActiveTabId() {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return tab?.id || null;
  }

  function waitForContentReady(tabId, timeout = 5000) {
    if (state.readyTabs.has(tabId)) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      let settled = false;
      const intervalId = setInterval(() => {
        if (!settled && state.readyTabs.has(tabId)) {
          settled = true;
          clearInterval(intervalId);
          clearTimeout(timeoutId);
          resolve(true);
        }
      }, 50);
      const timeoutId = setTimeout(() => {
        if (!settled) {
          settled = true;
          clearInterval(intervalId);
          resolve(state.readyTabs.has(tabId));
        }
      }, timeout);
    });
  }

  async function sendToContentScript(tabId, msg, frameId) {
    const maxAttempts = 3;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      try {
        const options = { frameId };
        const documentId = state.tabDocIds.get(tabId);
        if (documentId) {
          options.documentId = documentId;
        }
        return await chrome.tabs.sendMessage(
          tabId,
          { __emunium__: true, ...msg },
          options
        );
      } catch (error) {
        clearTrackedTab(tabId);
        if (attempt < maxAttempts - 1) {
          await waitForContentReady(tabId, 5000);
          continue;
        }
        return {
          id: msg.id,
          result: { error: "Content script error: " + error.message },
        };
      }
    }

    return {
      id: msg.id,
      result: { error: "Content script error: max retries exceeded" },
    };
  }

  async function resolveTabId(msg) {
    return (
      msg.tabId ||
      msg.params?.tabId ||
      state.pinnedTabId ||
      (await getActiveTabId())
    );
  }

  Object.assign(scope, {
    state,
    clearKeepAlive,
    clearPinnedTab,
    clearReconnect,
    clearTrackedTab,
    connect,
    getActiveTabId,
    resolveTabId,
    scheduleReconnect,
    send,
    sendToContentScript,
    startKeepAlive,
    waitForContentReady,
  });
})();
