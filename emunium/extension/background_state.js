// background_state.js - connection lifecycle and shared worker state (bridge protocol v2).
// Contract: docs/architecture/bridge-protocol.md sections 1-5.
(() => {
  "use strict";

  const scope = (globalThis.EmuniumBackground = globalThis.EmuniumBackground || {});

  const PROTOCOL = 2;
  const WS_CONNECTING = 0;
  const WS_OPEN = 1;

  // Mutable so tests can shorten waits; read at use time.
  const timing = scope.timing || {
    configRetryMs: 1000,
    backoffMinMs: 1000,
    backoffMaxMs: 5000,
    keepAliveMs: 20000,
    welcomeTimeoutMs: 10000,
    timeoutGraceMs: 1000,
    defaultGuardMs: 60000,
    injectionGuardMs: 15000,
    // UTF-8 bytes per reply frame; the Python peer's websockets max_size is 64 MiB.
    maxReplyBytes: 60 * 1024 * 1024,
  };

  const state = scope.state || {
    ws: null,
    welcomed: false,
    connecting: null,
    config: null,
    reconnectTimer: null,
    keepAliveTimer: null,
    welcomeTimer: null,
    backoffMs: timing.backoffMinMs,
    // tabId -> documentId of the top-frame document whose content script said ready
    // (true when Chrome gave no documentId).
    readyTabs: new Map(),
    // tabId -> documentId that already received content_main.js (MAIN world).
    mainWorldTabs: new Map(),
  };

  const KNOWN_CODES = new Set([
    "stale_element",
    "no_target",
    "target_closed",
    "content_unavailable",
    "navigation_timeout",
    "navigation_failed",
    "timeout",
    "wait_timeout",
    "script_error",
    "csp_blocked",
    "invalid_argument",
    "unknown_condition",
    "unknown_method",
    "internal",
  ]);

  function codedError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
  }

  function errorPayload(error) {
    const code = error && typeof error.code === "string" && KNOWN_CODES.has(error.code) ? error.code : "internal";
    let message = "";
    if (error && typeof error.message === "string") {
      message = error.message;
    } else if (error !== undefined && error !== null) {
      message = String(error);
    }
    return { code, message };
  }

  function errorMessage(error) {
    return String((error && error.message) || error || "");
  }

  // The single source of connection settings. Attach mode extends this function only.
  async function loadConfig() {
    let data;
    try {
      const response = await fetch(chrome.runtime.getURL("session.json") + "?t=" + Date.now(), {
        cache: "no-store",
      });
      if (!response.ok) {
        return null;
      }
      data = await response.json();
    } catch {
      return null;
    }
    if (!data || typeof data !== "object" || data.protocol !== PROTOCOL) {
      return null;
    }
    const port = data.port;
    if (!Number.isInteger(port) || port <= 0 || port > 65535 || typeof data.token !== "string" || !data.token) {
      return null;
    }
    return { url: `ws://127.0.0.1:${port}`, token: data.token, protocol: PROTOCOL };
  }

  function clearReconnect() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  }

  function scheduleReconnect(delayMs) {
    if (state.reconnectTimer) {
      return;
    }
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      connect();
    }, delayMs);
  }

  // 1 s, 2 s, 4 s, then 5 s until a welcome resets it.
  function takeBackoff() {
    const delay = Math.min(Math.max(state.backoffMs, timing.backoffMinMs), timing.backoffMaxMs);
    state.backoffMs = Math.min(delay * 2, timing.backoffMaxMs);
    return delay;
  }

  function resetBackoff() {
    state.backoffMs = timing.backoffMinMs;
  }

  function stopTimers() {
    if (state.keepAliveTimer) {
      clearInterval(state.keepAliveTimer);
      state.keepAliveTimer = null;
    }
    if (state.welcomeTimer) {
      clearTimeout(state.welcomeTimer);
      state.welcomeTimer = null;
    }
  }

  function isLive(socket) {
    return !!socket && (socket.readyState === WS_CONNECTING || socket.readyState === WS_OPEN);
  }

  function isConnected() {
    return state.welcomed && !!state.ws && state.ws.readyState === WS_OPEN;
  }

  function connect() {
    if (state.connecting) {
      return state.connecting;
    }
    if (isLive(state.ws)) {
      return Promise.resolve(true);
    }
    clearReconnect();
    state.connecting = (async () => {
      try {
        const config = await loadConfig();
        if (!config) {
          scheduleReconnect(timing.configRetryMs);
          return false;
        }
        let socket;
        try {
          socket = new WebSocket(config.url);
        } catch {
          scheduleReconnect(takeBackoff());
          return false;
        }
        state.ws = socket;
        state.welcomed = false;
        state.config = config;
        attachSocket(socket, config);
        return true;
      } finally {
        state.connecting = null;
      }
    })();
    return state.connecting;
  }

  // Watchdog entry point: no-op while connected, connecting, or waiting out a backoff.
  function ensureConnected() {
    if (state.connecting || state.reconnectTimer || isLive(state.ws)) {
      return;
    }
    connect();
  }

  function attachSocket(socket, config) {
    socket.onopen = () => {
      if (socket !== state.ws) {
        return;
      }
      const manifest = chrome.runtime.getManifest ? chrome.runtime.getManifest() : {};
      const userAgent = typeof navigator !== "undefined" && navigator ? String(navigator.userAgent || "") : "";
      try {
        socket.send(
          JSON.stringify({
            type: "hello",
            protocol: config.protocol,
            token: config.token,
            extensionVersion: (manifest && manifest.version) || "",
            userAgent,
          })
        );
      } catch {
        closeSocket(socket, 1000, "hello failed");
        return;
      }
      state.welcomeTimer = setTimeout(() => {
        state.welcomeTimer = null;
        if (socket === state.ws && !state.welcomed) {
          closeSocket(socket, 1000, "no welcome");
        }
      }, timing.welcomeTimeoutMs);
    };

    socket.onmessage = (event) => {
      if (socket !== state.ws) {
        return;
      }
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!frame || typeof frame !== "object") {
        return;
      }
      if (!state.welcomed) {
        if (frame.type !== "welcome") {
          return; // nothing is answered before the welcome
        }
        if (frame.protocol !== PROTOCOL) {
          closeSocket(socket, 4000, "protocol mismatch");
          return;
        }
        onWelcome(socket);
        return;
      }
      if (frame.type === "request" && typeof scope.handleRequest === "function") {
        scope.handleRequest(socket, frame);
      }
    };

    socket.onerror = () => {};

    socket.onclose = () => {
      if (socket !== state.ws) {
        return;
      }
      stopTimers();
      state.ws = null;
      state.welcomed = false;
      scheduleReconnect(takeBackoff());
    };
  }

  function onWelcome(socket) {
    stopTimers();
    state.welcomed = true;
    resetBackoff();
    clearReconnect();
    state.keepAliveTimer = setInterval(() => {
      if (socket === state.ws && state.welcomed) {
        sendFrame(socket, { type: "event", event: "ping" });
      }
    }, timing.keepAliveMs);
  }

  function closeSocket(socket, code, reason) {
    try {
      socket.close(code, reason);
    } catch {
      // already closing
    }
  }

  // Writes only on the current, welcomed, open socket (replies to a dead socket are dropped).
  function sendText(socket, text) {
    if (!socket || socket !== state.ws || !state.welcomed || socket.readyState !== WS_OPEN) {
      return false;
    }
    try {
      socket.send(text);
      return true;
    } catch {
      return false;
    }
  }

  function sendFrame(socket, frame) {
    let text;
    try {
      text = JSON.stringify(frame);
    } catch {
      return false;
    }
    return sendText(socket, text);
  }

  function sendEvent(name, fields) {
    return sendFrame(state.ws, Object.assign({ type: "event", event: name }, fields || {}));
  }

  function isMissingTab(error) {
    return /No tab with id/i.test(errorMessage(error));
  }

  async function getTabOrClosed(tabId) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (!tab) {
        throw codedError("target_closed", `tab ${tabId} no longer exists`);
      }
      return tab;
    } catch (error) {
      if (error && error.code) {
        throw error;
      }
      if (isMissingTab(error)) {
        throw codedError("target_closed", `tab ${tabId} no longer exists`);
      }
      throw error;
    }
  }

  async function activeTabOfLastFocusedNormalWindow() {
    let win = null;
    try {
      win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    } catch {
      win = null;
    }
    if (win && (win.type === undefined || win.type === "normal")) {
      const [tab] = await chrome.tabs.query({ active: true, windowId: win.id });
      if (tab) {
        return tab;
      }
    }
    const [fallback] = await chrome.tabs.query({ active: true, windowType: "normal" });
    return fallback || null;
  }

  // target.tabId, else the active tab of the last focused normal window. No worker pin.
  async function resolveTargetTab(target) {
    const tabId = target && Number.isInteger(target.tabId) ? target.tabId : null;
    if (tabId !== null) {
      return getTabOrClosed(tabId);
    }
    const tab = await activeTabOfLastFocusedNormalWindow();
    if (!tab || !Number.isInteger(tab.id)) {
      throw codedError("no_target", "no active tab in a normal window");
    }
    return tab;
  }

  function markContentReady(tabId, documentId) {
    state.readyTabs.set(tabId, typeof documentId === "string" && documentId ? documentId : true);
  }

  // documentId of the tab's current top-frame document, or null when Chrome cannot say.
  async function topDocumentId(tabId) {
    try {
      const frame = await chrome.webNavigation.getFrame({ tabId, frameId: 0 });
      return frame && typeof frame.documentId === "string" ? frame.documentId : null;
    } catch {
      return null;
    }
  }

  async function isContentReady(tabId) {
    const entry = state.readyTabs.get(tabId);
    if (entry === undefined) {
      return false;
    }
    if (typeof entry !== "string") {
      return true;
    }
    const current = await topDocumentId(tabId);
    return current === null ? true : current === entry;
  }

  // A new top-frame document invalidates readiness and the MAIN-world injection.
  function onTopFrameCommitted(tabId, documentId) {
    for (const map of [state.readyTabs, state.mainWorldTabs]) {
      const entry = map.get(tabId);
      if (entry !== undefined && (typeof documentId !== "string" || entry !== documentId)) {
        map.delete(tabId);
      }
    }
  }

  function clearTrackedTab(tabId) {
    state.readyTabs.delete(tabId);
    state.mainWorldTabs.delete(tabId);
  }

  Object.assign(scope, {
    PROTOCOL,
    KNOWN_CODES,
    timing,
    state,
    codedError,
    errorPayload,
    errorMessage,
    loadConfig,
    connect,
    ensureConnected,
    isConnected,
    clearReconnect,
    scheduleReconnect,
    takeBackoff,
    resetBackoff,
    sendText,
    sendFrame,
    sendEvent,
    isMissingTab,
    getTabOrClosed,
    resolveTargetTab,
    markContentReady,
    topDocumentId,
    isContentReady,
    onTopFrameCommitted,
    clearTrackedTab,
  });
})();
