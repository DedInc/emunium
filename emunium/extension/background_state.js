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
  // attach-mode.md section 3: the claim queue holds at most 64 nonces (oldest dropped).
  const MAX_PENDING_CLAIMS = 64;
  // Close code for a failed attach handshake (bad challenge/serverProof/welcomeMac, frame out of order).
  const CLOSE_AUTH = 4005;
  const NONCE_BYTES = 32;
  const MAC_BYTES = 32;
  // Frames that arrive while a welcome's welcomeMac is being verified are replayed after it.
  const MAX_EARLY_FRAMES = 64;
  const HUB_URL_RE = /^ws:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/?$/;

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
    // Attach mode: the server's challenge must be the first frame, within 5 s of open.
    challengeTimeoutMs: 5000,
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
    challengeTimer: null,
    backoffMs: timing.backoffMinMs,
    // tabId -> documentId of the top-frame document whose content script said ready
    // (true when Chrome gave no documentId).
    readyTabs: new Map(),
    // Attach-mode claims waiting for a welcome: [{nonce, hubPort, at}].
    pendingClaims: [],
    // Nonces already taken from a navigation (insertion-ordered, bounded).
    seenClaimNonces: new Set(),
    // The worker-start scan of open tabs for claim pages has run (attach mode).
    startClaimScanDone: false,
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
  // Firefox answers a fetch of a missing packaged file with 200 and an empty body, or (Firefox 154)
  // resolves it `ok` and rejects the body read with AbortError: both mean absent.
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
    let text;
    try {
      text = await response.text();
    } catch {
      return { present: false, data: null };
    }
    if (typeof text !== "string" || text.trim() === "") {
      return { present: false, data: null };
    }
    try {
      return { present: true, data: JSON.parse(text) };
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

  // hubUrl must be exactly ws://127.0.0.1:<port>[/] with an explicit port; returns the port or null.
  // Matched on the raw string: URL parsing would hide a missing port (and drop an explicit :80).
  function hubPortOf(hubUrl) {
    const match = typeof hubUrl === "string" ? HUB_URL_RE.exec(hubUrl) : null;
    if (!match) {
      return null;
    }
    const port = Number(match[1]);
    return port <= 65535 ? port : null;
  }

  let lastConfigWarning = null;

  // Logs an invalid packaged config once per distinct message (the retry loop runs every second).
  function warnConfig(message) {
    if (message === lastConfigWarning) {
      return;
    }
    lastConfigWarning = message;
    try {
      console.warn("emunium: " + message);
    } catch {
      // no console
    }
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

  // A build with attach.json always runs in attach mode (an invalid one is retried, never bypassed);
  // session.json is honoured only when no attach.json exists.
  async function loadFileConfig() {
    // Both are read in parallel; the precedence is decided below, not by fetch order.
    const [session, attach] = await Promise.all([readPackagedJson("session.json"), readPackagedJson("attach.json")]);
    if (attach.present) {
      const config = attachFileConfigFrom(attach.data);
      if (!config) {
        warnConfig("attach.json is invalid (protocol 2, mode attach, hubUrl ws://127.0.0.1:<port>, enrollment); retrying");
      }
      return config;
    }
    return session.present ? launchConfigFrom(session.data) : null;
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

  // ── attach handshake crypto (attach-mode.md section 3) ──────────────────
  // HMAC-SHA-256 via Web Crypto; strings are UTF-8; nonces and MACs are base64url without padding.

  function base64UrlEncode(bytes) {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // Uint8Array, or null when the text is not unpadded base64url.
  function base64UrlDecode(text) {
    if (typeof text !== "string" || !/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) {
      return null;
    }
    let binary;
    try {
      binary = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
    } catch {
      return null;
    }
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  function randomNonce() {
    const bytes = new Uint8Array(NONCE_BYTES);
    crypto.getRandomValues(bytes);
    return base64UrlEncode(bytes);
  }

  async function hmacSha256(secret, message) {
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
      "sign",
    ]);
    return new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
  }

  // Compares every byte (XOR accumulation, no early exit); lengths are fixed and public.
  function constantTimeEqual(a, b) {
    const length = Math.max(a.length, b.length);
    let diff = a.length ^ b.length;
    for (let i = 0; i < length; i += 1) {
      diff |= (a[i] | 0) ^ (b[i] | 0);
    }
    return diff === 0;
  }

  // MAC input: label|sN|cN|P[|extra...], P being the dialed port as a decimal string.
  function handshakeMessage(label, serverNonce, clientNonce, port, ...extra) {
    return [label, serverNonce, clientNonce, String(port), ...extra].join("|");
  }

  // Step 4 hello: proves the enrollment secret (and token) without sending either; bound to the port.
  async function buildAttachHello(config, serverNonce, clientNonce) {
    const port = config.hubPort;
    const proof = await hmacSha256(
      config.enrollment,
      handshakeMessage("emunium-client", serverNonce, clientNonce, port, config.installId)
    );
    const tokenProof = config.token
      ? base64UrlEncode(await hmacSha256(config.token, handshakeMessage("emunium-token", serverNonce, clientNonce, port)))
      : null;
    return {
      type: "hello",
      protocol: config.protocol,
      mode: "attach",
      installId: config.installId,
      proof: base64UrlEncode(proof),
      tokenProof,
    };
  }

  // A received MAC (32 bytes, unpadded base64url) against HMAC(secret, message), compared in constant time.
  async function verifyMac(secret, message, received) {
    const bytes = base64UrlDecode(received);
    if (!bytes || bytes.length !== MAC_BYTES) {
      return false;
    }
    return constantTimeEqual(bytes, await hmacSha256(secret, message));
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
    if (state.challengeTimer) {
      clearTimeout(state.challengeTimer);
      state.challengeTimer = null;
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
        if (config.mode === "attach" && !state.startClaimScanDone) {
          // A claim page may have committed before the onCommitted listener existed.
          state.startClaimScanDone = true;
          scanClaimTabs(config).catch(() => {});
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

  // Adds extensionVersion/userAgent, sends the hello and arms the welcome timeout.
  function sendHello(socket, hello) {
    const manifest = chrome.runtime.getManifest ? chrome.runtime.getManifest() : {};
    hello.extensionVersion = (manifest && manifest.version) || "";
    hello.userAgent = typeof navigator !== "undefined" && navigator ? String(navigator.userAgent || "") : "";
    try {
      socket.send(JSON.stringify(hello));
    } catch {
      closeSocket(socket, 1000, "hello failed");
      return false;
    }
    state.welcomeTimer = setTimeout(() => {
      state.welcomeTimer = null;
      if (socket === state.ws && !state.welcomed) {
        closeSocket(socket, 1000, "no welcome");
      }
    }, timing.welcomeTimeoutMs);
    return true;
  }

  function handleWelcomedFrame(socket, frame) {
    if (frame.type === "request" && typeof scope.handleRequest === "function") {
      scope.handleRequest(socket, frame);
    }
  }

  // Per-socket attach handshake (attach-mode.md section 3), the server proves itself first:
  // idle -> challenge -> serverProof -> verifyingServer -> proving -> welcome -> verifyingWelcome -> done | failed.
  // Before the serverProof verifies, only the clientNonce frame is sent; nothing is stored or executed
  // before the welcomeMac verifies. Any frame out of this order closes 4005.
  function attachHandshake(socket, config) {
    let phase = "idle";
    let serverNonce = null;
    let clientNonce = null;
    let early = [];

    const current = () => socket === state.ws && socket.readyState === WS_OPEN && !state.welcomed;

    function clearPhaseTimer() {
      if (state.challengeTimer) {
        clearTimeout(state.challengeTimer);
        state.challengeTimer = null;
      }
    }

    function fail(reason) {
      phase = "failed";
      early = [];
      if (socket === state.ws) {
        stopTimers();
      }
      closeSocket(socket, CLOSE_AUTH, reason);
    }

    // Fails the handshake when it is still in `expected` after ms.
    function armPhaseTimer(expected, ms, reason) {
      clearPhaseTimer();
      state.challengeTimer = setTimeout(() => {
        state.challengeTimer = null;
        if (phase === expected && socket === state.ws) {
          fail(reason);
        }
      }, ms);
    }

    function open() {
      phase = "challenge";
      armPhaseTimer("challenge", timing.challengeTimeoutMs, "no challenge");
    }

    // Steps 1-2: a well-formed challenge is answered with the clientNonce frame and nothing else.
    function onChallenge(frame) {
      clearPhaseTimer();
      const isChallenge = !!frame && frame.type === "challenge" && frame.protocol === PROTOCOL;
      const nonceBytes = isChallenge ? base64UrlDecode(frame.serverNonce) : null;
      if (!nonceBytes || nonceBytes.length !== NONCE_BYTES) {
        fail("bad challenge");
        return;
      }
      serverNonce = frame.serverNonce;
      try {
        clientNonce = randomNonce();
        socket.send(JSON.stringify({ type: "clientNonce", clientNonce }));
      } catch {
        fail("clientNonce failed");
        return;
      }
      phase = "serverProof";
      armPhaseTimer("serverProof", timing.welcomeTimeoutMs, "no server proof");
    }

    // Steps 3-4: the hello is built and sent only after the port-bound serverProof verified.
    function onServerProof(frame) {
      clearPhaseTimer();
      if (!frame || frame.type !== "serverProof") {
        fail("expected serverProof");
        return;
      }
      phase = "verifyingServer";
      const message = handshakeMessage("emunium-server", serverNonce, clientNonce, config.hubPort);
      verifyMac(config.enrollment, message, frame.serverProof)
        .then((ok) => {
          if (phase !== "verifyingServer" || !current()) {
            return null;
          }
          if (!ok) {
            fail("bad server proof");
            return null;
          }
          phase = "proving";
          return buildAttachHello(config, serverNonce, clientNonce).then((hello) => {
            if (phase !== "proving" || !current()) {
              return;
            }
            phase = "welcome";
            sendHello(socket, hello);
          });
        })
        .catch(() => {
          if ((phase === "verifyingServer" || phase === "proving") && socket === state.ws) {
            fail("handshake failed");
          }
        });
    }

    // Steps 5-6: the welcomeMac binds nonces, port, installId and token; verified before anything else.
    function onWelcomeFrame(frame) {
      if (!frame || frame.type !== "welcome") {
        fail("expected welcome");
        return;
      }
      const token = frame.token === undefined || frame.token === null ? "" : frame.token;
      if (frame.protocol !== PROTOCOL || frame.installId !== config.installId || typeof token !== "string") {
        fail("bad welcome");
        return;
      }
      phase = "verifyingWelcome";
      const message = handshakeMessage(
        "emunium-welcome",
        serverNonce,
        clientNonce,
        config.hubPort,
        config.installId,
        token
      );
      verifyMac(config.enrollment, message, frame.welcomeMac).then(
        (ok) => {
          if (phase !== "verifyingWelcome" || !current()) {
            return;
          }
          if (!ok) {
            fail("bad welcome mac");
            return;
          }
          phase = "done";
          const replay = early;
          early = [];
          // A token is stored only from a welcome whose welcomeMac (covering that token) verified.
          if (token) {
            storeToken(token);
          }
          onWelcome(socket);
          for (const queued of replay) {
            if (socket !== state.ws || !state.welcomed) {
              break;
            }
            handleWelcomedFrame(socket, queued);
          }
        },
        () => {
          if (phase === "verifyingWelcome" && socket === state.ws) {
            fail("handshake failed");
          }
        }
      );
    }

    function onFrame(frame) {
      switch (phase) {
        case "challenge":
          onChallenge(frame);
          return;
        case "serverProof":
          onServerProof(frame);
          return;
        case "verifyingServer":
        case "proving":
          fail("frame before hello"); // a genuine hub waits for the hello
          return;
        case "welcome":
          onWelcomeFrame(frame);
          return;
        case "verifyingWelcome":
          // Requests the hub sends right after its welcome run once the welcomeMac checks out.
          if (frame && early.length < MAX_EARLY_FRAMES) {
            early.push(frame);
          }
          return;
        default:
          return;
      }
    }

    return { open, onFrame, done: () => phase === "done" };
  }

  function attachSocket(socket, config) {
    const handshake = config.mode === "attach" ? attachHandshake(socket, config) : null;

    socket.onopen = () => {
      if (socket !== state.ws) {
        return;
      }
      if (handshake) {
        handshake.open(); // the server speaks first
        return;
      }
      sendHello(socket, { type: "hello", protocol: config.protocol, token: config.token });
    };

    socket.onmessage = (event) => {
      if (socket !== state.ws) {
        return;
      }
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        frame = null;
      }
      if (!frame || typeof frame !== "object") {
        frame = null;
      }
      if (handshake && !handshake.done()) {
        handshake.onFrame(frame);
        return;
      }
      if (!frame) {
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
      handleWelcomedFrame(socket, frame);
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
    if (attachClaimTarget(state.config) !== null) {
      scanClaimTabs(state.config).catch(() => {});
    }
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
    return queueClaim(details.url, details.tabId, config);
  }

  // Queues a claim URL once per nonce, sends it when welcomed, and blanks its tab.
  async function queueClaim(url, tabId, config) {
    const hubPort = attachClaimTarget(config);
    if (hubPort === null) {
      return false;
    }
    const nonce = parseClaimUrl(url, hubPort);
    if (nonce === null || state.seenClaimNonces.has(nonce)) {
      return false;
    }
    rememberNonce(nonce);
    state.pendingClaims.push({ nonce, hubPort, at: Date.now() });
    while (state.pendingClaims.length > MAX_PENDING_CLAIMS) {
      state.pendingClaims.shift(); // a page can mint claim URLs; keep only the newest
    }
    flushClaims();
    if (Number.isInteger(tabId)) {
      try {
        await chrome.tabs.update(tabId, { url: "about:blank" });
      } catch {
        // tab already gone or navigated away: the nonce is captured either way
      }
    }
    return true;
  }

  // Already-open tabs showing a claim page (committed before the listener existed); returns the count queued.
  async function scanClaimTabs(config) {
    const target = config || state.config;
    if (attachClaimTarget(target) === null) {
      return 0;
    }
    let tabs;
    try {
      tabs = await chrome.tabs.query({});
    } catch {
      return 0;
    }
    const queued = await Promise.all(
      (Array.isArray(tabs) ? tabs : []).map((tab) =>
        tab && typeof tab.url === "string" ? queueClaim(tab.url, tab.id, target) : false
      )
    );
    return queued.filter(Boolean).length;
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
    scanClaimTabs,
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
