// background_handlers.js - request dispatch (bridge protocol v2, sections 2, 3 and 6).
// Every request gets exactly one reply: handlers throw coded errors, handleRequest replies.
(() => {
  "use strict";

  const scope = globalThis.EmuniumBackground;
  const { codedError, errorMessage, timing } = scope;

  const CONTENT_FILES = [
    "content_shared.js",
    "content_queries.js",
    "content_conditions.js",
    "content_wait.js",
    "content.js",
  ];
  const MAIN_WORLD_FILE = "content_main.js";
  const WAIT_UNTIL = new Set(["commit", "domcontentloaded", "load"]);
  const CONTENT_METHODS = new Set([
    "ping",
    "pageInfo",
    "querySelector",
    "querySelectorAll",
    "queryXPath",
    "queryByText",
    "getAllInteractive",
    "describeElement",
    "scrollIntoView",
    "scrollTo",
    "focus",
    "focusState",
    "pointerProbe",
    "getAttribute",
    "getComputedStyle",
    "waitForSelector",
  ]);
  const DEFAULT_NAVIGATION_TIMEOUT_MS = 30000;
  const DEFAULT_SCRIPT_TIMEOUT_MS = 30000;
  const DEFAULT_RESPONSE_TIMEOUT_MS = 10000;

  // ── parameter helpers ───────────────────────────────────────────────────

  function requireString(value, name) {
    if (typeof value !== "string" || !value) {
      throw codedError("invalid_argument", `'${name}' must be a non-empty string`);
    }
    return value;
  }

  function timeoutParam(value, fallback) {
    if (value === undefined || value === null) {
      return fallback;
    }
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
      throw codedError("invalid_argument", "'timeoutMs' must be a non-negative number");
    }
    return value;
  }

  function waitUntilParam(value) {
    const waitUntil = value === undefined || value === null ? "load" : value;
    if (!WAIT_UNTIL.has(waitUntil)) {
      throw codedError("invalid_argument", `'waitUntil' must be one of commit, domcontentloaded, load`);
    }
    return waitUntil;
  }

  function sinceParam(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function delay(ms, onElapsed) {
    let timer = null;
    const promise = new Promise((resolve, reject) => {
      timer = setTimeout(() => {
        try {
          resolve(onElapsed());
        } catch (error) {
          reject(error);
        }
      }, ms);
    });
    return { promise, cancel: () => clearTimeout(timer) };
  }

  async function race(work, ms, makeError) {
    const timer = delay(ms, () => {
      throw makeError();
    });
    try {
      return await Promise.race([work, timer.promise]);
    } finally {
      timer.cancel();
    }
  }

  // ── navigation ──────────────────────────────────────────────────────────

  function normalizeUrl(value) {
    if (typeof value !== "string") {
      return null;
    }
    try {
      return new URL(value).href;
    } catch {
      return value;
    }
  }

  // Listeners are attached before the navigation starts; events for a tab id that
  // is not bound yet (createTab) are buffered and replayed by bind().
  // Only events after our own top-frame onBeforeNavigate count (commit, errors,
  // redirects); same-document completions count only for the target URL. Everything
  // else belongs to the previous page.
  function watchNavigation({ url, waitUntil, timeoutMs }) {
    let tabId = null;
    let committed = false;
    let startedUrl = null;
    let done = false;
    const targetUrl = normalizeUrl(url);
    // Normalized URLs of our navigation: its start URL and every main-frame redirect target.
    const ownUrls = new Set();
    const buffered = [];
    const listeners = [];
    let settle = null;
    const promise = new Promise((resolve, reject) => {
      settle = (error, value) => (error ? reject(error) : resolve(value));
    });
    promise.catch(() => {});

    const cleanup = () => {
      for (const [event, fn] of listeners) {
        event.removeListener(fn);
      }
      listeners.length = 0;
      timer.cancel();
    };
    const finish = (error, value) => {
      if (done) {
        return;
      }
      done = true;
      cleanup();
      settle(error, value);
    };

    const handle = (kind, id, details) => {
      if (done) {
        return;
      }
      if (tabId === null) {
        buffered.push([kind, id, details]);
        return;
      }
      if (id !== tabId) {
        return;
      }
      switch (kind) {
        case "before":
          if (startedUrl === null) {
            startedUrl = details.url;
          }
          ownUrls.add(normalizeUrl(details.url));
          break;
        case "redirect":
          if (startedUrl !== null && typeof details.redirectUrl === "string") {
            ownUrls.add(normalizeUrl(details.redirectUrl));
          }
          break;
        case "committed":
          if (startedUrl === null) {
            break; // a commit of the previous page's own navigation
          }
          committed = true;
          if (waitUntil === "commit") {
            finish(null, "commit");
          }
          break;
        case "dcl":
          if (committed && waitUntil === "domcontentloaded") {
            finish(null, "domcontentloaded");
          }
          break;
        case "load":
          // Requiring our commit first ignores a late onCompleted of the previous page.
          if (committed) {
            finish(null, "load");
          }
          break;
        case "same-document":
          // history.pushState/replaceState of the old page must not end our wait.
          if (normalizeUrl(details.url) === targetUrl) {
            finish(null, "same-document");
          }
          break;
        case "error": {
          const failedUrl = details.url;
          if (startedUrl === null) {
            break; // our navigation has not started: the previous one failed
          }
          // An ERR_ABORTED for a URL outside our chain is the navigation we superseded.
          if (details.error === "net::ERR_ABORTED" && !ownUrls.has(normalizeUrl(failedUrl))) {
            break;
          }
          finish(codedError("navigation_failed", `navigation to ${failedUrl || url} failed: ${details.error}`));
          break;
        }
        case "removed":
          finish(codedError("target_closed", `tab ${tabId} was closed during navigation`));
          break;
        default:
          break;
      }
    };

    const on = (event, fn, ...extra) => {
      if (event && typeof event.addListener === "function") {
        event.addListener(fn, ...extra);
        listeners.push([event, fn]);
      }
    };
    const nav = chrome.webNavigation || {};
    const top = (kind) => (details) => {
      if (details && details.frameId === 0) {
        handle(kind, details.tabId, details);
      }
    };
    on(nav.onBeforeNavigate, top("before"));
    on(nav.onCommitted, top("committed"));
    on(nav.onDOMContentLoaded, top("dcl"));
    on(nav.onCompleted, top("load"));
    on(nav.onErrorOccurred, top("error"));
    on(nav.onReferenceFragmentUpdated, top("same-document"));
    on(nav.onHistoryStateUpdated, top("same-document"));
    on(chrome.tabs.onUpdated, (id, change) => {
      if (change && change.status === "complete") {
        handle("load", id, {});
      }
    });
    on(chrome.tabs.onRemoved, (id) => handle("removed", id, {}));
    const webRequest = chrome.webRequest || {};
    on(
      webRequest.onBeforeRedirect,
      (details) => {
        if (details && (details.type === "main_frame" || details.frameId === 0)) {
          handle("redirect", details.tabId, details);
        }
      },
      { urls: ["<all_urls>"], types: ["main_frame"] }
    );

    const timer = delay(timeoutMs, () => {
      finish(
        codedError(
          "navigation_timeout",
          `navigation to ${url} did not reach '${waitUntil}' within ${timeoutMs} ms`
        )
      );
    });

    return {
      promise,
      bind(id) {
        tabId = id;
        for (const [kind, eventTabId, details] of buffered.splice(0)) {
          handle(kind, eventTabId, details);
        }
      },
      cancel() {
        if (!done) {
          done = true;
          cleanup();
        }
      },
    };
  }

  function tabApiError(error, tabId) {
    if (error && error.code) {
      return error;
    }
    if (scope.isMissingTab(error)) {
      return codedError("target_closed", `tab ${tabId} no longer exists`);
    }
    return error;
  }

  // tabs.update/create reject bad URLs (invalid, file:// without access, javascript:).
  function navigationApiError(error, tabId, url) {
    const mapped = tabApiError(error, tabId);
    if (mapped && mapped.code) {
      return mapped;
    }
    const message = errorMessage(error);
    if (/No current window/i.test(message)) {
      return codedError("no_target", `no window to open ${url} in: ${message}`);
    }
    if (/invalid url|cannot navigate|not allowed|unsupported|file:/i.test(message)) {
      return codedError("invalid_argument", `cannot navigate to ${url}: ${message}`);
    }
    return error; // anything else is reported as internal
  }

  async function handleNavigate(ctx) {
    const url = requireString(ctx.params.url, "url");
    const waitUntil = waitUntilParam(ctx.params.waitUntil);
    const timeoutMs = timeoutParam(ctx.params.timeoutMs, DEFAULT_NAVIGATION_TIMEOUT_MS);
    const tab = await scope.resolveTargetTab(ctx.target);
    const tabId = tab.id;
    ctx.tabId = tabId;

    const watcher = watchNavigation({ url, waitUntil, timeoutMs });
    watcher.bind(tabId);
    try {
      await chrome.tabs.update(tabId, { url });
    } catch (error) {
      watcher.cancel();
      throw navigationApiError(error, tabId, url);
    }
    await watcher.promise;
    const after = await scope.getTabOrClosed(tabId);
    return {
      url: after.url,
      title: after.title,
      tabId,
      contentReady: await scope.isContentReady(tabId),
    };
  }

  async function handleCreateTab(ctx) {
    const url = ctx.params.url === undefined || ctx.params.url === null ? "about:blank" : requireString(ctx.params.url, "url");
    const active = ctx.params.active !== false;
    const waitUntil = waitUntilParam(ctx.params.waitUntil);
    const timeoutMs = timeoutParam(ctx.params.timeoutMs, DEFAULT_NAVIGATION_TIMEOUT_MS);
    const shouldWait = url !== "about:blank";

    const watcher = shouldWait ? watchNavigation({ url, waitUntil, timeoutMs }) : null;
    let tab;
    try {
      tab = await chrome.tabs.create({ url, active });
    } catch (error) {
      if (watcher) {
        watcher.cancel();
      }
      throw navigationApiError(error, null, url);
    }
    ctx.tabId = tab.id;
    if (watcher) {
      watcher.bind(tab.id);
      await watcher.promise;
    }
    const after = await scope.getTabOrClosed(tab.id);
    return {
      tabId: tab.id,
      url: after.url || after.pendingUrl || url,
      title: after.title,
      contentReady: await scope.isContentReady(tab.id),
    };
  }

  // ── tabs ────────────────────────────────────────────────────────────────

  async function handleGetTabInfo(ctx) {
    const tab = await scope.resolveTargetTab(ctx.target);
    ctx.tabId = tab.id;
    return {
      tabId: tab.id,
      url: tab.url,
      title: tab.title,
      status: tab.status,
      active: !!tab.active,
      windowId: tab.windowId,
    };
  }

  async function handleListTabs() {
    const tabs = await chrome.tabs.query({ windowType: "normal" });
    return tabs.map((tab) => ({
      tabId: tab.id,
      url: tab.url,
      title: tab.title,
      active: !!tab.active,
      windowId: tab.windowId,
    }));
  }

  async function handleCloseTab(ctx) {
    const tab = await scope.resolveTargetTab(ctx.target);
    ctx.tabId = tab.id;
    try {
      await chrome.tabs.remove(tab.id);
    } catch (error) {
      throw tabApiError(error, tab.id);
    }
    scope.clearTrackedTab(tab.id);
    return { closed: true };
  }

  async function handleActivateTab(ctx) {
    const tab = await scope.resolveTargetTab(ctx.target);
    ctx.tabId = tab.id;
    try {
      await chrome.tabs.update(tab.id, { active: true });
    } catch (error) {
      throw tabApiError(error, tab.id);
    }
    try {
      await chrome.windows.update(tab.windowId, { focused: true });
    } catch {
      // the tab is active even when the window manager refuses focus
    }
    const after = await scope.getTabOrClosed(tab.id);
    return { tabId: tab.id, windowId: after.windowId, activated: !!after.active };
  }

  // ── executeScript ───────────────────────────────────────────────────────

  // Serialized by chrome.scripting and run in the page MAIN world: must stay self-contained.
  // `compiled` is set by the userScripts fallback, which ships the code as source instead.
  async function runPageScript(code, compiled) {
    const MAX_NODES = 1000000;
    const MAX_CHARS = 32 * 1024 * 1024;
    const UNSERIALIZABLE = "[Unserializable]";
    const TOO_LARGE = { tooLarge: true }; // sentinel: the only thing walk() lets escape

    // Never throws except TOO_LARGE: hostile getters, proxies and toJSON become "[Unserializable]".
    const toJSONSafe = (input) => {
      const ancestors = new Set();
      let nodes = 0;
      let chars = 0;
      const spend = (count, size) => {
        nodes += count;
        chars += size;
        if (nodes > MAX_NODES || chars > MAX_CHARS) {
          throw TOO_LARGE;
        }
      };
      const read = (object, key) => {
        try {
          return { ok: true, value: object[key] };
        } catch {
          return { ok: false, value: undefined };
        }
      };
      const walk = (value, depth) => {
        try {
          return walkValue(value, depth);
        } catch (error) {
          if (error === TOO_LARGE) {
            throw error;
          }
          return UNSERIALIZABLE;
        }
      };
      const walkValue = (value, depth) => {
        spend(1, 8);
        if (value === null || value === undefined) {
          return null;
        }
        const kind = typeof value;
        if (kind === "string") {
          spend(0, value.length);
          return value;
        }
        if (kind === "boolean") {
          return value;
        }
        if (kind === "number") {
          return Number.isFinite(value) ? value : null;
        }
        if (kind === "bigint") {
          const text = value.toString();
          spend(0, text.length);
          return text;
        }
        if (kind === "function" || kind === "symbol") {
          return null;
        }
        if (depth > 64) {
          return null;
        }
        if (ancestors.has(value)) {
          return "[Circular]";
        }
        if (typeof Node !== "undefined" && value instanceof Node) {
          return {
            nodeType: value.nodeType,
            nodeName: value.nodeName,
            id: value.id || null,
            className: typeof value.className === "string" ? value.className : null,
          };
        }
        const toJSON = value.toJSON;
        if (typeof toJSON === "function") {
          return walk(toJSON.call(value), depth + 1);
        }
        if (value instanceof Error) {
          return { name: walk(value.name, depth + 1), message: walk(value.message, depth + 1) };
        }
        if (typeof window !== "undefined" && value === window) {
          return "[Window]";
        }
        ancestors.add(value);
        try {
          if (value instanceof Map) {
            const out = [];
            for (const [key, item] of value) {
              out.push([walk(key, depth + 1), walk(item, depth + 1)]);
            }
            return out;
          }
          if (value instanceof Set) {
            const out = [];
            for (const item of value) {
              out.push(walk(item, depth + 1));
            }
            return out;
          }
          if (
            Array.isArray(value) ||
            ArrayBuffer.isView(value) ||
            (typeof value.length === "number" && typeof value.item === "function")
          ) {
            const length = value.length;
            if (!Number.isInteger(length) || length < 0) {
              return UNSERIALIZABLE;
            }
            if (nodes + length > MAX_NODES) {
              throw TOO_LARGE; // sparse giants are rejected before iterating
            }
            const out = [];
            for (let index = 0; index < length; index += 1) {
              const slot = read(value, index);
              out.push(slot.ok ? walk(slot.value, depth + 1) : UNSERIALIZABLE);
            }
            return out;
          }
          const out = {};
          for (const key of Object.keys(value)) {
            const slot = read(value, key);
            if (slot.ok && (slot.value === undefined || typeof slot.value === "function" || typeof slot.value === "symbol")) {
              continue;
            }
            spend(0, key.length + 4);
            // defineProperty keeps "__proto__" as an own key instead of setting the prototype.
            Object.defineProperty(out, key, {
              value: slot.ok ? walk(slot.value, depth + 1) : UNSERIALIZABLE,
              enumerable: true,
              writable: true,
              configurable: true,
            });
          }
          return out;
        } finally {
          ancestors.delete(value);
        }
      };
      return walk(input, 0);
    };

    let fn = compiled;
    if (typeof fn !== "function") {
      try {
        fn = new Function(code);
      } catch (error) {
        const message = String((error && error.message) || error);
        if (
          (typeof EvalError !== "undefined" && error instanceof EvalError) ||
          /Content Security Policy|unsafe-eval|TrustedScript|Trusted Type|Code generation from strings/i.test(message)
        ) {
          return { status: "csp", message };
        }
        return { status: "error", name: (error && error.name) || "Error", message };
      }
    }
    let value;
    try {
      value = await fn();
    } catch (error) {
      let name = "Error";
      let message = "";
      try {
        name = String((error && error.name) || "Error");
        message = String((error && error.message) || error);
      } catch {
        message = UNSERIALIZABLE;
      }
      return { status: "error", name, message };
    }
    try {
      return { status: "ok", value: toJSONSafe(value) };
    } catch (error) {
      if (error === TOO_LARGE) {
        return { status: "too_large", message: `result too large (over ${MAX_NODES} values or ~32 MiB)` };
      }
      return { status: "ok", value: UNSERIALIZABLE };
    }
  }

  function scriptingError(error, tabId) {
    if (error && error.code) {
      return error;
    }
    if (scope.isMissingTab(error)) {
      return codedError("target_closed", `tab ${tabId} no longer exists`);
    }
    return codedError("content_unavailable", `cannot script tab ${tabId}: ${errorMessage(error)}`);
  }

  function scriptOutcome(results) {
    const first = Array.isArray(results) ? results.find((r) => r && r.frameId === 0) || results[0] : null;
    const outcome = first ? first.result : undefined;
    if (!outcome || typeof outcome !== "object" || typeof outcome.status !== "string") {
      throw codedError("script_error", "script produced no result (syntax error or the page navigated away)");
    }
    return outcome;
  }

  function userScriptSource(code) {
    return `(${runPageScript.toString()})(null, function () {\n${code}\n})`;
  }

  async function runViaUserScripts(tabId, code) {
    const api = chrome.userScripts;
    if (!api || typeof api.execute !== "function") {
      return null;
    }
    let results;
    try {
      results = await api.execute({
        target: { tabId, frameIds: [0] },
        world: "MAIN",
        injectImmediately: true,
        js: [{ code: userScriptSource(code) }],
      });
    } catch (error) {
      if (scope.isMissingTab(error)) {
        throw codedError("target_closed", `tab ${tabId} no longer exists`);
      }
      return null; // userScripts not enabled for this profile: report the CSP block
    }
    return scriptOutcome(results);
  }

  async function runScript(tabId, code) {
    let results;
    try {
      results = await chrome.scripting.executeScript({
        target: { tabId, frameIds: [0] },
        world: "MAIN",
        args: [code, null],
        func: runPageScript,
      });
    } catch (error) {
      throw scriptingError(error, tabId);
    }
    let outcome = scriptOutcome(results);
    if (outcome.status === "csp") {
      const fallback = await runViaUserScripts(tabId, code);
      if (!fallback || fallback.status === "csp") {
        throw codedError("csp_blocked", `page Content Security Policy blocks script evaluation: ${outcome.message}`);
      }
      outcome = fallback;
    }
    if (outcome.status === "too_large") {
      throw codedError("script_error", outcome.message);
    }
    if (outcome.status === "error") {
      throw codedError("script_error", `${outcome.name}: ${outcome.message}`);
    }
    return { value: outcome.value === undefined ? null : outcome.value };
  }

  async function handleExecuteScript(ctx) {
    const code = ctx.params.code;
    if (typeof code !== "string") {
      throw codedError("invalid_argument", "'code' must be a string");
    }
    const timeoutMs = timeoutParam(ctx.params.timeoutMs, DEFAULT_SCRIPT_TIMEOUT_MS);
    const tab = await scope.resolveTargetTab(ctx.target);
    ctx.tabId = tab.id;
    return race(runScript(tab.id, code), timeoutMs, () =>
      codedError("timeout", `executeScript did not finish within ${timeoutMs} ms`)
    );
  }

  // ── network (implemented in background_network.js) ──────────────────────

  async function networkTabId(ctx, anyTab) {
    if (anyTab) {
      const tabId = ctx.target && Number.isInteger(ctx.target.tabId) ? ctx.target.tabId : null;
      ctx.tabId = tabId;
      return tabId;
    }
    const tab = await scope.resolveTargetTab(ctx.target);
    ctx.tabId = tab.id;
    return tab.id;
  }

  async function handleWaitForResponse(ctx) {
    const pattern = requireString(ctx.params.pattern, "pattern");
    const timeoutMs = timeoutParam(ctx.params.timeoutMs, DEFAULT_RESPONSE_TIMEOUT_MS);
    const anyTab = ctx.params.anyTab === true;
    const since = sinceParam(ctx.params.since);
    const tabId = await networkTabId(ctx, anyTab);
    const entry = await scope.waitForResponse({ pattern, timeoutMs, tabId, since, anyTab });
    return entry === undefined ? null : entry;
  }

  async function handleGetRecentResponses(ctx) {
    const anyTab = ctx.params.anyTab === true;
    const since = sinceParam(ctx.params.since);
    const tabId = await networkTabId(ctx, anyTab);
    const entries = await scope.getRecentResponses({ since, tabId, anyTab });
    return Array.isArray(entries) ? entries : [];
  }

  // ── content-routed ──────────────────────────────────────────────────────

  function needsMainWorld(value, depth = 0) {
    if (!value || typeof value !== "object" || depth > 32) {
      return false;
    }
    if (Array.isArray(value)) {
      return value.some((item) => needsMainWorld(item, depth + 1));
    }
    if (value.type === "custom_js") {
      return true;
    }
    return Object.keys(value).some((key) => needsMainWorld(value[key], depth + 1));
  }

  function isNoReceiver(error) {
    return /Receiving end does not exist|Could not establish connection/i.test(errorMessage(error));
  }

  async function injectContentScripts(tabId) {
    try {
      await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: CONTENT_FILES });
    } catch (error) {
      throw scriptingError(error, tabId);
    }
  }

  // Cached per top-frame documentId read at check time; the cache is written only when
  // the document is unchanged across the injection. Returns true when it injected.
  async function ensureMainWorld(tabId) {
    const before = await scope.topDocumentId(tabId);
    if (before !== null && scope.state.mainWorldTabs.get(tabId) === before) {
      return false;
    }
    let results;
    try {
      results = await chrome.scripting.executeScript({
        target: { tabId, frameIds: [0] },
        world: "MAIN",
        files: [MAIN_WORLD_FILE],
      });
    } catch (error) {
      throw scriptingError(error, tabId);
    }
    const first = Array.isArray(results) ? results[0] : null;
    const injected = first && typeof first.documentId === "string" ? first.documentId : before;
    const after = await scope.topDocumentId(tabId);
    if (after !== null && injected === after && before === after) {
      scope.state.mainWorldTabs.set(tabId, after);
    } else {
      scope.state.mainWorldTabs.delete(tabId); // document changed mid-injection: verify next time
    }
    return true;
  }

  async function sendToTopFrame(tabId, message) {
    return chrome.tabs.sendMessage(tabId, message, { frameId: 0 });
  }

  function contentSendError(error, tabId) {
    if (scope.isMissingTab(error)) {
      return codedError("target_closed", `tab ${tabId} no longer exists`);
    }
    if (isNoReceiver(error)) {
      return codedError("content_unavailable", `no content script in tab ${tabId} after injection`);
    }
    return codedError("content_unavailable", `content script in tab ${tabId} did not answer: ${errorMessage(error)}`);
  }

  async function forwardToContent(ctx) {
    const tab = await scope.resolveTargetTab(ctx.target);
    const tabId = tab.id;
    ctx.tabId = tabId;
    ctx.wantZoom = true;
    // After any injection the guard restarts, so content waits end in their own wait_timeout.
    if (ctx.method === "waitForSelector" && needsMainWorld(ctx.params.conditions)) {
      ctx.holdGuard();
      try {
        await ensureMainWorld(tabId);
      } finally {
        ctx.rearmGuard();
      }
    }
    const message = { __emunium__: 2, id: ctx.id, method: ctx.method, params: ctx.params };
    let response;
    try {
      response = await sendToTopFrame(tabId, message);
    } catch (error) {
      if (!isNoReceiver(error)) {
        throw contentSendError(error, tabId);
      }
      ctx.holdGuard();
      try {
        await injectContentScripts(tabId);
      } finally {
        ctx.rearmGuard();
      }
      try {
        response = await sendToTopFrame(tabId, message);
      } catch (retryError) {
        throw contentSendError(retryError, tabId);
      }
    }
    if (!response || typeof response !== "object" || typeof response.ok !== "boolean") {
      throw codedError("content_unavailable", `content script in tab ${tabId} sent no response`);
    }
    if (!response.ok) {
      const error = response.error || {};
      throw codedError(
        typeof error.code === "string" ? error.code : "internal",
        typeof error.message === "string" ? error.message : ""
      );
    }
    return response.result === undefined ? null : response.result;
  }

  // ── dispatch ────────────────────────────────────────────────────────────

  const WORKER_METHODS = {
    navigate: handleNavigate,
    getTabInfo: handleGetTabInfo,
    listTabs: handleListTabs,
    createTab: handleCreateTab,
    closeTab: handleCloseTab,
    activateTab: handleActivateTab,
    executeScript: handleExecuteScript,
    waitForResponse: handleWaitForResponse,
    getRecentResponses: handleGetRecentResponses,
  };

  function pickHandler(method) {
    if (Object.prototype.hasOwnProperty.call(WORKER_METHODS, method)) {
      return WORKER_METHODS[method];
    }
    if (CONTENT_METHODS.has(method)) {
      return forwardToContent;
    }
    return null;
  }

  function guardBudget(params) {
    const value = params && params.timeoutMs;
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      return value + timing.timeoutGraceMs;
    }
    return timing.defaultGuardMs;
  }

  async function zoomOf(tabId) {
    try {
      const zoom = await chrome.tabs.getZoom(tabId);
      return typeof zoom === "number" ? zoom : null;
    } catch {
      return null;
    }
  }

  async function runRequest(ctx) {
    const handler = pickHandler(ctx.method);
    if (!handler) {
      throw codedError("unknown_method", `unknown method '${ctx.method}'`);
    }
    const budget = guardBudget(ctx.params);
    let timer = null;
    let expire = null;
    const guard = new Promise((resolve, reject) => {
      expire = () => reject(codedError("timeout", `${ctx.method} did not finish within ${budget} ms`));
    });
    const arm = (ms) => {
      clearTimeout(timer);
      timer = setTimeout(expire, ms);
    };
    // holdGuard: an injection is running (bounded separately); rearmGuard: it finished,
    // so the request's own budget starts again.
    ctx.holdGuard = () => arm(Math.max(budget, timing.injectionGuardMs));
    ctx.rearmGuard = () => arm(budget);
    arm(budget);
    try {
      return await Promise.race([Promise.resolve().then(() => handler(ctx)), guard]);
    } finally {
      clearTimeout(timer);
      ctx.holdGuard = () => {};
      ctx.rearmGuard = () => {};
    }
  }

  async function handleRequest(socket, frame) {
    const params = frame.params && typeof frame.params === "object" && !Array.isArray(frame.params) ? frame.params : {};
    const target = frame.target && typeof frame.target === "object" ? frame.target : { tabId: null, frameId: 0 };
    const ctx = {
      id: frame.id,
      method: frame.method,
      params,
      target,
      tabId: null,
      wantZoom: false,
      holdGuard: () => {},
      rearmGuard: () => {},
    };
    let reply;
    try {
      const result = await runRequest(ctx);
      reply = { type: "reply", id: ctx.id, ok: true, result: result === undefined ? null : result, tabId: ctx.tabId };
    } catch (error) {
      reply = { type: "reply", id: ctx.id, ok: false, error: scope.errorPayload(error), tabId: ctx.tabId };
    }
    try {
      if (ctx.wantZoom && Number.isInteger(ctx.tabId)) {
        reply.zoom = await zoomOf(ctx.tabId);
      }
    } catch {
      // zoom is best effort; the reply still goes out
    }
    const encoded = encodeReply(ctx, reply);
    scope.sendText(socket, encoded.text);
    return encoded.reply;
  }

  function utf8Length(text) {
    // UTF-8 never exceeds 3 bytes per UTF-16 unit: skip the encode for small frames.
    if (text.length * 3 <= timing.maxReplyBytes) {
      return text.length;
    }
    return new TextEncoder().encode(text).length;
  }

  // Serializes once and measures UTF-8 bytes: an oversized frame would exceed the peer's
  // 64 MiB max_size and drop the socket for every pending request.
  function encodeReply(ctx, reply) {
    let text = null;
    let problem = null;
    try {
      text = JSON.stringify(reply);
      const bytes = utf8Length(text);
      if (bytes > timing.maxReplyBytes) {
        problem = `${bytes} bytes, limit ${timing.maxReplyBytes}`;
      }
    } catch (error) {
      problem = `not serializable: ${errorMessage(error)}`;
    }
    if (problem === null) {
      return { reply, text };
    }
    const tooLarge = ctx.method === "executeScript"
      ? { code: "script_error", message: `result too large (${problem})` }
      : { code: "internal", message: `reply too large (${problem})` };
    const replacement = { type: "reply", id: reply.id, ok: false, error: tooLarge, tabId: reply.tabId };
    if ("zoom" in reply) {
      replacement.zoom = reply.zoom;
    }
    return { reply: replacement, text: JSON.stringify(replacement) };
  }

  Object.assign(scope, {
    CONTENT_FILES,
    MAIN_WORLD_FILE,
    handleRequest,
    forwardToContent,
    needsMainWorld,
    runPageScript,
    watchNavigation,
  });
})();
