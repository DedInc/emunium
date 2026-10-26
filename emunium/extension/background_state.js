// background_state.js - connection lifecycle and shared worker state (bridge protocol v2).
// Contract: docs/architecture/bridge-protocol.md sections 1-5, docs/architecture/attach-mode.md sections 2-4.
(() => {
  "use strict";

  const scope = (globalThis.EmuniumBackground = globalThis.EmuniumBackground || {});

  const PROTOCOL = 2;
  const WS_CONNECTING = 0;
  const WS_OPEN = 1;
  const LOOPBACK = "127.0.0.1";
  const CLAIM_NONCE_RE = /^[A-Za-z0-9_-]{16,128}$/;
  const MAX_SEEN_NONCES = 256;

  // Mutable so tests can shorten waits; read at use time.
  const timing = scope.timing || {
    configRetryMs: 1000,
    backoffMinMs: 1000,
    backoffMaxMs: 5000,
    // Attach mode: a long-running hub may be down for a while; back off further.
    attachBackoffMaxMs: 30000,
    // A start-URL claim nonce is single-use with a 5 min TTL on the hub.
    claimTtlMs: 5 * 60 * 1000,
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
    // Attach-mode claims waiting for a welcome: [{nonce, hubPort, at}].
    pendingClaims: [],
    // Nonces already taken from a navigation (insertion-ordered, bounded).
    seenClaimNonces: new Set(),
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

  // {present, data}: present is false when the packaged file does not exist (or cannot be read).
  async function readPackagedJson(name) {
    let response;
    try {
      response = await fetch(chrome.runtime.getURL(name) + "?t=" + Date.now(), { cache: "no-store" });
    } catch {
      return { present: false, data: null };
    }
    if (!response || !response.ok) {
      return { present: false, data: null };
    }
    try {
      return { present: true, data: await response.json() };
    } catch {
      return { present: true, data: null };
    }
  }

  function launchConfigFrom(data) {
    if (!data || typeof data !== "object" || data.protocol !== PROTOCOL) {
      return null;
    }
    const port = data.port;
    if (!Number.isInteger(port) || port <= 0 || port > 65535 || typeof data.token !== "string" || !data.token) {
      return null;
    }
    return { url: `ws://127.0.0.1:${port}`, token: data.token, protocol: PROTOCOL };
  }

  // hubUrl must be exactly ws://127.0.0.1[:port][/]; returns the numeric port or null.
  function hubPortOf(hubUrl) {
    if (typeof hubUrl !== "string") {
      return null;
    }
    let parsed;
    try {
      parsed = new URL(hubUrl);
    } catch {
      return null;
    }
    if (
      parsed.protocol !== "ws:" ||
      parsed.hostname !== LOOPBACK ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
    const port = parsed.port === "" ? 80 : Number(parsed.port);
    return Number.isInteger(port) && port > 0 && port <= 65535 ? port : null;
  }

  // attach.json without the per-profile identity: {mode, url, hubPort, enrollment, protocol}.
  function attachFileConfigFrom(data) {
    if (!data || typeof data !== "object" || data.protocol !== PROTOCOL || data.mode !== "attach") {
      return null;
    }
    const hubPort = hubPortOf(data.hubUrl);
    if (hubPort === null || typeof data.enrollment !== "string" || !data.enrollment) {
      return null;
    }
    return { mode: "attach", url: `ws://${LOOPBACK}:${hubPort}`, hubPort, enrollment: data.enrollment, protocol: PROTOCOL };
  }

  // session.json wins when present (launch mode, unchanged); otherwise attach.json.
  async function loadFileConfig() {
    const session = await readPackagedJson("session.json");
    if (session.present) {
      return launchConfigFrom(session.data);
    }
    const attach = await readPackagedJson("attach.json");
    return attach.present ? attachFileConfigFrom(attach.data) : null;
  }

  let identityCreation = null;

  function isUuid(value) {
    return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
  }

  // {installId, token}: installId is created once per profile and kept in chrome.storage.local.
  async function loadIdentity() {
    const stored = (await chrome.storage.local.get(["installId", "token"])) || {};
    const token = typeof stored.token === "string" && stored.token ? stored.token : null;
    if (isUuid(stored.installId)) {
      return { installId: stored.installId, token };
    }
    if (!identityCreation) {
      // Single-flight: concurrent callers must not each mint a different id.
      identityCreation = (async () => {
        const installId = crypto.randomUUID();
        await chrome.storage.local.set({ installId });
        return installId;
      })().finally(() => {
        identityCreation = null;
      });
    }
    return { installId: await identityCreation, token };
  }

  // The single source of connection settings.
  async function loadConfig() {
    const config = await loadFileConfig();
    if (!config || config.mode !== "attach") {
      return config;
    }
    let identity;
    try {
      identity = await loadIdentity();
    } catch {
      return null; // storage unavailable: retry like a missing config
    }
    return Object.assign(config, identity);
  }

  function storeToken(token) {
    if (state.config && state.config.mode === "attach") {
      state.config.token = token;
    }
    try {
      const stored = chrome.storage.local.set({ token });
      if (stored && typeof stored.catch === "function") {
        stored.catch(() => {});
      }
    } catch {
      // the next hello sends the old token; the hub then answers 4001 and needs hub.forget()
    }
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

  // Launch: 1 s, 2 s, 4 s, then 5 s; attach: doubling up to 30 s. A welcome resets it.
  function takeBackoff(config) {
    const mode = (config || state.config || {}).mode;
    const max = mode === "attach" ? timing.attachBackoffMaxMs : timing.backoffMaxMs;
    const delay = Math.min(Math.max(state.backoffMs, timing.backoffMinMs), max);
    state.backoffMs = Math.min(delay * 2, max);
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
          scheduleReconnect(takeBackoff(config));
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
      const hello =
        config.mode === "attach"
          ? {
              type: "hello",
              protocol: config.protocol,
              mode: "attach",
              installId: config.installId,
              enrollment: config.enrollment,
              token: config.token || null,
            }
          : { type: "hello", protocol: config.protocol, token: config.token };
      hello.extensionVersion = (manifest && manifest.version) || "";
      hello.userAgent = userAgent;
      try {
        socket.send(JSON.stringify(hello));
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
        if (config.mode === "attach" && typeof frame.token === "string" && frame.token) {
          storeToken(frame.token);
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
      scheduleReconnect(takeBackoff(config));
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
    flushClaims();
  }

  // ── start-URL claim (attach-mode.md section 4.1) ────────────────────────

  // The nonce of a committed URL that is exactly http://127.0.0.1:<hubPort>/pair#n=<nonce>, else null.
  function parseClaimUrl(url, hubPort) {
    if (typeof url !== "string" || !Number.isInteger(hubPort) || hubPort <= 0 || hubPort > 65535) {
      return null;
    }
    // Chrome commits canonical URLs, which drop the scheme's default port.
    const origin = hubPort === 80 ? `http://${LOOPBACK}` : `http://${LOOPBACK}:${hubPort}`;
    const prefix = `${origin}/pair#n=`;
    if (!url.startsWith(prefix)) {
      return null;
    }
    const nonce = url.slice(prefix.length);
    return CLAIM_NONCE_RE.test(nonce) ? nonce : null;
  }

  function attachClaimTarget(config) {
    return config && config.mode === "attach" && Number.isInteger(config.hubPort) ? config.hubPort : null;
  }

  function rememberNonce(nonce) {
    state.seenClaimNonces.add(nonce);
    while (state.seenClaimNonces.size > MAX_SEEN_NONCES) {
      state.seenClaimNonces.delete(state.seenClaimNonces.values().next().value);
    }
  }

  // Top-frame commit: a matching claim URL is queued once, sent when welcomed, and the tab is blanked.
  async function handleClaimNavigation(details) {
    if (!details || details.frameId !== 0 || typeof details.url !== "string") {
      return false;
    }
    if (!details.url.startsWith(`http://${LOOPBACK}`)) {
      return false; // cheap reject before touching the config
    }
    // Before the first connect the mode is unknown; read the files (no identity side effects).
    const config = state.config || (await loadFileConfig());
    const hubPort = attachClaimTarget(config);
    if (hubPort === null) {
      return false;
    }
    const nonce = parseClaimUrl(details.url, hubPort);
    if (nonce === null || state.seenClaimNonces.has(nonce)) {
      return false;
    }
    rememberNonce(nonce);
    state.pendingClaims.push({ nonce, hubPort, at: Date.now() });
    flushClaims();
    if (Number.isInteger(details.tabId)) {
      try {
        await chrome.tabs.update(details.tabId, { url: "about:blank" });
      } catch {
        // tab already gone or navigated away: the nonce is captured either way
      }
    }
    return true;
  }

  // Sends queued claims on the current welcomed attach socket; drops claims older than claimTtlMs.
  function flushClaims() {
    const now = Date.now();
    state.pendingClaims = state.pendingClaims.filter((claim) => now - claim.at <= timing.claimTtlMs);
    const hubPort = attachClaimTarget(state.config);
    if (!state.pendingClaims.length || hubPort === null || !isConnected()) {
      return;
    }
    const remaining = [];
    for (const claim of state.pendingClaims) {
      const sent = claim.hubPort === hubPort && sendEvent("claim", { nonce: claim.nonce });
      if (!sent) {
        remaining.push(claim);
      }
    }
    state.pendingClaims = remaining;
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

  // A new top-frame document invalidates readiness.
  function onTopFrameCommitted(tabId, documentId) {
    const entry = state.readyTabs.get(tabId);
    if (entry !== undefined && (typeof documentId !== "string" || entry !== documentId)) {
      state.readyTabs.delete(tabId);
    }
  }

  function clearTrackedTab(tabId) {
    state.readyTabs.delete(tabId);
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
    loadIdentity,
    hubPortOf,
    parseClaimUrl,
    handleClaimNavigation,
    flushClaims,
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
