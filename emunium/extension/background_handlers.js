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
    "elementText",
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
  // Mirrors content_wait.js / content_conditions.js: default wait timeout, nesting limit.
  const DEFAULT_WAIT_TIMEOUT_MS = 10000;
  const MAX_CONDITION_DEPTH = 32;
  const MAIN_EVALUATOR_GRACE_MS = 5000;
  const MAIN_EVALUATOR_MARGIN_MS = 2000;
  const MAX_TIMER_MS = 2147483647;

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

  // Firefox's NS_BINDING_ABORTED (0x804B0002), as webNavigation reports it ("Error code 2152398850").
  function isBindingAborted(error) {
    return typeof error === "string" && /2152398850|NS_BINDING_ABORTED/.test(error);
  }

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

  // Listeners are attached before the navigation starts; events are buffered until
  // bind() (after tabs.update/create resolved) and then replayed in arrival order.
  // Our navigation starts at the first top-frame onBeforeNavigate for the target URL
  // (or the tab's pendingUrl) that arrived after issue(); only events after it count
  // (commit, errors, redirects). Same-document completions count only for the target
  // URL. Earlier onBeforeNavigates are pre-existing navigations (e.g. Chromium's start
  // page still loading) that ours supersedes: their ERR_ABORTED is not our failure.
  function watchNavigation({ url, waitUntil, timeoutMs }) {
    let tabId = null;
    let issued = false;
    let committed = false;
    let startedUrl = null;
    let done = false;
    // Firefox reports NS_BINDING_ABORTED for a top-frame navigation that still commits and
    // loads: the failure is held and reported only if the wait times out.
    let heldAbort = null;
    const targetUrl = normalizeUrl(url);
    const startUrls = new Set([targetUrl]);
    // Normalized URLs of our navigation: its start URL and every main-frame redirect target.
    const ownUrls = new Set();
    // Current URLs of pre-existing navigations not yet ended by an error (one entry each).
    const foreignUrls = [];
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

    const handle = (kind, id, details, afterIssue = issued) => {
      if (done) {
        return;
      }
      if (tabId === null) {
        buffered.push([kind, id, details, afterIssue]);
        return;
      }
      if (id !== tabId) {
        return;
      }
      switch (kind) {
        case "before": {
          const beforeUrl = normalizeUrl(details.url);
          if (startedUrl === null) {
            if (!afterIssue || !startUrls.has(beforeUrl)) {
              foreignUrls.push(beforeUrl);
              break;
            }
            startedUrl = details.url;
          }
          // After our start, a further top-frame navigation (client redirect) is ours too.
          ownUrls.add(beforeUrl);
          break;
        }
        case "redirect": {
          if (typeof details.redirectUrl !== "string") {
            break;
          }
          const from = normalizeUrl(details.url);
          const foreign = foreignUrls.indexOf(from);
          if (foreign !== -1) {
            foreignUrls[foreign] = normalizeUrl(details.redirectUrl);
          } else if (startedUrl !== null && ownUrls.has(from)) {
            ownUrls.add(normalizeUrl(details.redirectUrl));
          }
          break;
        }
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
          const foreign = foreignUrls.indexOf(normalizeUrl(failedUrl));
          if (foreign !== -1) {
            foreignUrls.splice(foreign, 1); // a pre-existing navigation ended (superseded by ours)
            break;
          }
          if (startedUrl === null) {
            break; // our navigation has not started: the previous one failed
          }
          // An ERR_ABORTED for a URL outside our chain is the navigation we superseded.
          if (details.error === "net::ERR_ABORTED" && !ownUrls.has(normalizeUrl(failedUrl))) {
            break;
          }
          const failure = codedError("navigation_failed", `navigation to ${failedUrl || url} failed: ${details.error}`);
          if (isBindingAborted(details.error)) {
            heldAbort = heldAbort || failure;
            break;
          }
          finish(failure);
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
        heldAbort ||
          codedError(
            "navigation_timeout",
            `navigation to ${url} did not reach '${waitUntil}' within ${timeoutMs} ms`
          )
      );
    });

    return {
      promise,
      // Call right before tabs.update/create: earlier navigations cannot be ours.
      issue() {
        issued = true;
      },
      // tab: the tabs.update/create result; its pendingUrl is our URL as Chrome canonicalized it.
      bind(id, tab) {
        if (tab && typeof tab.pendingUrl === "string" && tab.pendingUrl) {
          startUrls.add(normalizeUrl(tab.pendingUrl));
        }
        tabId = id;
        for (const [kind, eventTabId, details, afterIssue] of buffered.splice(0)) {
          handle(kind, eventTabId, details, afterIssue);
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
    let updated;
    try {
      watcher.issue();
      updated = await chrome.tabs.update(tabId, { url });
    } catch (error) {
      watcher.cancel();
      throw navigationApiError(error, tabId, url);
    }
    watcher.bind(tabId, updated);
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
      if (watcher) {
        watcher.issue();
      }
      tab = await chrome.tabs.create({ url, active });
    } catch (error) {
      if (watcher) {
        watcher.cancel();
      }
      throw navigationApiError(error, null, url);
    }
    ctx.tabId = tab.id;
    if (watcher) {
      watcher.bind(tab.id, tab);
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
        // Async so the body may `await`; under a CSP this constructor throws EvalError too.
        const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
        fn = new AsyncFunction(code);
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
    return `(${runPageScript.toString()})(null, async function () {\n${code}\n})`;
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

  // The custom_js sources of a conditions tree, in the order of content_conditions.js
  // customJsLeaves (walkLeaves: depth-first, groups in order): code i is evaluated through
  // event type `${channel}:${i}`. null when the tree is one the content script rejects
  // anyway (too deep, non-string code), so nothing is injected for it.
  function customJsCodes(conditions) {
    const codes = [];
    let valid = true;
    const walk = (list, depth) => {
      if (!Array.isArray(list) || !valid) return;
      if (depth > MAX_CONDITION_DEPTH) {
        valid = false;
        return;
      }
      for (const condition of list) {
        if (!condition) continue;
        if (condition.type === "any_of" || condition.type === "all_of") {
          for (const group of Array.isArray(condition.conditions) ? condition.conditions : []) walk(group, depth + 1);
        } else if (condition.type === "not") {
          walk(condition.condition, depth + 1);
        } else if (condition.type === "custom_js") {
          if (typeof condition.code !== "string") valid = false;
          codes.push(condition.code);
        }
      }
    };
    walk(conditions, 0);
    return valid ? codes : null;
  }

  // Serialized by chrome.scripting and run in the page MAIN world: must stay self-contained.
  // One evaluator per wait. The random channel is the only name involved: listeners are
  // capturing, on `document`, for `${channel}:${i}` (evaluate code i for the event's
  // target; preventDefault() means true), `${channel}:${i}:async` (preventDefault() means
  // the last result of code i was a thenable), `${channel}:ready` (preventDefault() means
  // the evaluator is alive) and `${channel}:close`. Nothing is written to window, attributes
  // or event details; everything is removed on close or after ttlMs. `precompiled` (one
  // function per code) is set by the userScripts fallback, which ships the codes as source.
  function mainWorldEvaluator(channel, codes, ttlMs, precompiled) {
    "use strict";
    const describeError = (error) => String((error && error.message) || error);
    const isCodegenBlocked = (error) =>
      (typeof EvalError !== "undefined" && error instanceof EvalError) ||
      Boolean(error && error.name === "EvalError") ||
      /Content Security Policy|unsafe-eval|TrustedScript|Trusted Type|Code generation from strings/i.test(describeError(error));
    if (typeof channel !== "string" || channel === "" || !Array.isArray(codes)) {
      return { ok: false, code: "script_error", message: "invalid custom_js evaluator arguments" };
    }
    const count = codes.length;
    const compiled = [];
    for (let index = 0; index < count; index += 1) {
      if (Array.isArray(precompiled)) {
        if (typeof precompiled[index] !== "function") {
          return { ok: false, code: "script_error", message: "invalid custom_js evaluator arguments" };
        }
        compiled[index] = precompiled[index];
        continue;
      }
      try {
        compiled[index] = new Function("el", codes[index]);
      } catch (error) {
        return { ok: false, code: isCodegenBlocked(error) ? "csp_blocked" : "script_error", message: describeError(error) };
      }
    }
    // Captured at injection, so page patches made AFTER injection do not change the
    // evaluator, and the channel avoids Array/String/Boolean methods below. This is not
    // isolation: a page that patched MAIN-world built-ins (addEventListener, Reflect.apply,
    // Event.prototype...) BEFORE injection can learn the channel and forge results. MV3
    // isolated worlds refuse string compilation, so custom_js cannot run anywhere safer.
    const doc = document;
    const listen = doc.addEventListener;
    const unlisten = doc.removeEventListener;
    const EventProto = Event.prototype;
    const preventDefault = EventProto.preventDefault;
    const stopImmediatePropagation = EventProto.stopImmediatePropagation;
    const composedPath = EventProto.composedPath;
    const apply = Reflect.apply;
    const PromiseCtor = Promise;
    const promiseCatch = Promise.prototype.catch;
    const startTimer = setTimeout;
    const stopTimer = clearTimeout;

    const registeredTypes = [];
    const registeredFns = [];
    let registeredCount = 0;
    const thenable = [];
    let ttlTimer = null;
    const on = (type, fn) => {
      apply(listen, doc, [type, fn, true]);
      registeredTypes[registeredCount] = type;
      registeredFns[registeredCount] = fn;
      registeredCount += 1;
    };
    const close = () => {
      for (let i = 0; i < registeredCount; i += 1) apply(unlisten, doc, [registeredTypes[i], registeredFns[i], true]);
      registeredCount = 0;
      if (ttlTimer !== null) stopTimer(ttlTimer);
      ttlTimer = null;
    };
    // composedPath()[0] is the dispatch target even inside open shadow roots; a dispatch on
    // the document itself means "no element".
    const elementOf = (event) => {
      const path = apply(composedPath, event, []);
      const node = path && path.length ? path[0] : event.target;
      return node && node.nodeType === 1 ? node : null;
    };

    for (let index = 0; index < count; index += 1) {
      const fn = compiled[index];
      thenable[index] = false;
      on(`${channel}:${index}`, (event) => {
        apply(stopImmediatePropagation, event, []);
        thenable[index] = false;
        let truthy = false;
        try {
          const result = apply(fn, undefined, [elementOf(event)]);
          if (
            result !== null &&
            (typeof result === "object" || typeof result === "function") &&
            typeof result.then === "function"
          ) {
            if (result instanceof PromiseCtor) apply(promiseCatch, result, [() => {}]);
            thenable[index] = true; // custom_js must return synchronously
          } else {
            truthy = !!result;
          }
        } catch {
          truthy = false; // runtime exceptions mean "not yet"
        }
        if (truthy) apply(preventDefault, event, []);
      });
      on(`${channel}:${index}:async`, (event) => {
        apply(stopImmediatePropagation, event, []);
        if (thenable[index]) apply(preventDefault, event, []);
      });
    }
    on(`${channel}:ready`, (event) => {
      apply(stopImmediatePropagation, event, []);
      apply(preventDefault, event, []);
    });
    on(`${channel}:close`, (event) => {
      apply(stopImmediatePropagation, event, []);
      close();
    });
    // setTimeout fires at once above 2^31-1 ms.
    const ttl = typeof ttlMs === "number" && ttlMs >= 0 ? (ttlMs > 2147483647 ? 2147483647 : ttlMs) : 15000;
    ttlTimer = startTimer(close, ttl);
    return { ok: true };
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

  // The wait starts after the injection; the extra margin keeps the evaluator alive past
  // the content wait's final tick (at timeoutMs) despite messaging and injection delays.
  function mainEvaluatorTtl(timeoutMs) {
    const wait =
      typeof timeoutMs === "number" && Number.isFinite(timeoutMs) && timeoutMs >= 0 ? timeoutMs : DEFAULT_WAIT_TIMEOUT_MS;
    return Math.min(wait + MAIN_EVALUATOR_GRACE_MS + MAIN_EVALUATOR_MARGIN_MS, MAX_TIMER_MS);
  }

  // { outcome, documentId } from frame 0, or null when it produced no evaluator result.
  function evaluatorOutcome(results) {
    const first = Array.isArray(results) ? results.find((r) => r && r.frameId === 0) || results[0] : null;
    const outcome = first ? first.result : undefined;
    if (!outcome || typeof outcome !== "object" || typeof outcome.ok !== "boolean") {
      return null;
    }
    return { outcome, documentId: typeof first.documentId === "string" ? first.documentId : null };
  }

  function evaluatorError(outcome) {
    const message = typeof outcome.message === "string" ? outcome.message : "";
    if (outcome.code === "csp_blocked") {
      return codedError("csp_blocked", `page Content Security Policy blocks custom_js evaluation: ${message}`);
    }
    return codedError("script_error", `custom_js: ${message}`);
  }

  // The userScripts world is exempt from the page CSP, but `new Function` inside it is not:
  // the codes travel as function literals, the other arguments as JSON.
  function mainEvaluatorUserScript(channel, codes, ttlMs) {
    const functions = codes.map((code) => `function (el) {\n${code}\n}`).join(",\n");
    return (
      `(${mainWorldEvaluator.toString()})(` +
      `${JSON.stringify(channel)}, ${JSON.stringify(codes)}, ${JSON.stringify(ttlMs)}, [${functions}])`
    );
  }

  // undefined: userScripts unavailable (report the CSP block); null: no result.
  async function injectEvaluatorViaUserScripts(tabId, channel, codes, ttlMs) {
    const api = chrome.userScripts;
    if (!api || typeof api.execute !== "function") {
      return undefined;
    }
    let results;
    try {
      results = await api.execute({
        target: { tabId, frameIds: [0] },
        world: "MAIN",
        injectImmediately: true,
        js: [{ code: mainEvaluatorUserScript(channel, codes, ttlMs) }],
      });
    } catch (error) {
      if (scope.isMissingTab(error)) {
        throw codedError("target_closed", `tab ${tabId} no longer exists`);
      }
      return undefined; // userScripts not enabled for this profile
    }
    return evaluatorOutcome(results);
  }

  // One injection: { outcome, documentId } on success, null without a frame-0 result;
  // compile failures throw.
  async function installMainEvaluator(tabId, channel, codes, ttlMs) {
    let results;
    try {
      results = await chrome.scripting.executeScript({
        target: { tabId, frameIds: [0] },
        world: "MAIN",
        func: mainWorldEvaluator,
        args: [channel, codes, ttlMs],
      });
    } catch (error) {
      throw scriptingError(error, tabId);
    }
    const installed = evaluatorOutcome(results);
    if (installed === null || installed.outcome.ok) {
      return installed;
    }
    if (installed.outcome.code !== "csp_blocked") {
      throw evaluatorError(installed.outcome);
    }
    const fallback = await injectEvaluatorViaUserScripts(tabId, channel, codes, ttlMs);
    if (fallback === undefined) {
      throw evaluatorError(installed.outcome);
    }
    if (fallback === null) {
      // A syntax error in one code breaks the whole user script.
      throw codedError("script_error", "custom_js: the evaluator produced no result (syntax error or the page navigated away)");
    }
    if (!fallback.outcome.ok) {
      throw evaluatorError(fallback.outcome);
    }
    return fallback;
  }

  // Installs this wait's evaluator in frame 0. A compile failure is the wait's error, so
  // the wait is never forwarded. A navigation during the injection (no frame-0 result, or
  // a new top document) leaves the page without listeners: inject once more.
  async function injectMainEvaluator(tabId, channel, codes, ttlMs) {
    for (let attempt = 0; ; attempt += 1) {
      const installed = await installMainEvaluator(tabId, channel, codes, ttlMs);
      if (installed === null) {
        if (attempt >= 1) {
          throw codedError("content_unavailable", `the custom_js evaluator did not start in tab ${tabId}`);
        }
        continue;
      }
      const current = installed.documentId === null ? null : await scope.topDocumentId(tabId);
      if (current === null || current === installed.documentId || attempt >= 1) {
        return;
      }
    }
  }

  // waitForSelector params as forwarded: a caller-supplied __mainChannel is never trusted;
  // with custom_js the worker opens a fresh channel and installs its evaluator first.
  async function prepareWaitParams(ctx, tabId) {
    const params = Object.assign({}, ctx.params);
    delete params.__mainChannel;
    const codes = customJsCodes(params.conditions);
    if (!codes || codes.length === 0) {
      return params;
    }
    const channel = crypto.randomUUID();
    ctx.holdGuard();
    try {
      await injectMainEvaluator(tabId, channel, codes, mainEvaluatorTtl(params.timeoutMs));
    } finally {
      ctx.rearmGuard();
    }
    params.__mainChannel = channel;
    return params;
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
    const isWait = ctx.method === "waitForSelector";
    const params = isWait ? await prepareWaitParams(ctx, tabId) : ctx.params;
    let message = { __emunium__: 2, id: ctx.id, method: ctx.method, params };
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
      // No receiver usually means a new document, which lacks the evaluator injected
      // above: a fresh channel and evaluator for the re-send.
      if (isWait && params.__mainChannel !== undefined) {
        message = { __emunium__: 2, id: ctx.id, method: ctx.method, params: await prepareWaitParams(ctx, tabId) };
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
    customJsCodes,
    handleRequest,
    forwardToContent,
    mainEvaluatorTtl,
    mainWorldEvaluator,
    runPageScript,
    watchNavigation,
  });
})();
