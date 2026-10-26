(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;

  const DEFAULT_TIMEOUT_MS = 10000;
  const DEFAULT_STABLE_MS = 300;
  const POLL_INTERVAL_MS = 50;
  // Without DOM mutations the page is still re-checked this often (stable windows,
  // layout/style-only changes, custom_js reading non-DOM state).
  const IDLE_RECHECK_MS = 250;
  const MAX_TIMEOUT_MS = 2147483647;
  const SELECTOR_TYPES = new Set(["css", "xpath", "text"]);
  const STATES = new Set(["presence", "visible", "clickable", "stable", "unobscured"]);

  function codedError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
  }

  // Coded errors keep their code; an invalid selector/XPath is the caller's mistake.
  function toWaitError(error) {
    if (error && typeof error.code === "string") {
      return error;
    }
    const message = String((error && error.message) || error);
    if (error && error.name === "SyntaxError") {
      return codedError("invalid_argument", message);
    }
    return codedError("internal", `waitForSelector failed: ${message}`);
  }

  function buildAllFinder(selector, type) {
    if (type === "xpath") {
      return () => {
        let result;
        try {
          result = document.evaluate(
            selector,
            document,
            null,
            XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
            null
          );
        } catch (error) {
          // TypeError: the expression is not a node-set (e.g. count(//a)); SyntaxError /
          // NamespaceError: malformed expression. Both are the caller's argument.
          const name = error && error.name;
          if (name === "TypeError" || name === "SyntaxError" || name === "NamespaceError") {
            throw codedError("invalid_argument", `invalid XPath for waitForSelector: ${error.message}`);
          }
          throw error;
        }
        const nodes = [];
        for (let i = 0; i < result.snapshotLength; i += 1) {
          const node = result.snapshotItem(i);
          if (node && node.nodeType === 1) {
            nodes.push(node);
          }
        }
        return nodes;
      };
    }
    if (type === "text") {
      // Looked up at call time: content_shared.js may be replaced after this file loads.
      return () => Array.from(scope.findElementsByText(selector, false) || []);
    }
    return () => Array.from(document.querySelectorAll(selector));
  }

  function needsStableState(state, conditions) {
    return state === "stable" || scope.hasConditionType(conditions, "stable");
  }

  // Longest requested duration anywhere in the tree; 300 ms when none is given.
  function getStableDuration(conditions) {
    let duration = null;
    scope.walkLeaves(conditions, (c) => {
      if (c.type === "stable" && typeof c.duration === "number") {
        duration = duration === null ? c.duration : Math.max(duration, c.duration);
      }
    });
    return duration === null ? DEFAULT_STABLE_MS : duration;
  }

  // textContent, not innerText: innerText forces layout and builds the rendered text of a
  // whole container on every tick. Length + prefix detects changes cheaply.
  function fingerprint(element) {
    const rect = element.getBoundingClientRect();
    const text = element.textContent || "";
    return JSON.stringify({
      x: rect.x,
      y: rect.y,
      w: rect.width,
      h: rect.height,
      textLength: text.length,
      text: text.slice(0, 200),
      href: element.href || null,
      value: element.value !== undefined ? element.value : null,
      connected: element.isConnected,
      ready: document.readyState,
    });
  }

  function normalizeParams(params) {
    const { selector, type, timeoutMs, state, conditions } = params || {};
    if (typeof selector !== "string" || selector === "") {
      throw codedError("invalid_argument", "'selector' must be a non-empty string");
    }
    const selectorType = type == null ? "css" : type;
    if (!SELECTOR_TYPES.has(selectorType)) {
      throw codedError("invalid_argument", `'type' must be css, xpath or text, got ${JSON.stringify(type)}`);
    }
    const waitState = state == null ? "presence" : state;
    if (!STATES.has(waitState)) {
      throw codedError("invalid_argument", `unknown wait state ${JSON.stringify(state)}`);
    }
    const requested = timeoutMs ?? DEFAULT_TIMEOUT_MS;
    if (typeof requested !== "number" || Number.isNaN(requested) || requested < 0) {
      throw codedError("invalid_argument", "'timeoutMs' must be a number >= 0");
    }
    // setTimeout fires at once above 2^31-1 ms; clamp to ~24.8 days ("effectively forever").
    const timeout = Math.min(requested, MAX_TIMEOUT_MS);
    const validated = scope.validateConditions(conditions);
    return {
      conditions: validated,
      needsStable: needsStableState(waitState, validated),
      selector,
      selectorType,
      stableDuration: getStableDuration(validated),
      state: waitState,
      timeoutMs: timeout,
    };
  }

  function isOwnRecord(record) {
    return Boolean(scope.ownNodes && record && scope.ownNodes.has(record.target));
  }

  function waitForSelector(params) {
    let options;
    try {
      options = normalizeParams(params);
    } catch (error) {
      return Promise.reject(toWaitError(error));
    }
    const findAll = buildAllFinder(options.selector, options.selectorType);

    return new Promise((resolve, reject) => {
      let settled = false;
      let ticking = false;
      let observer = null;
      let intervalId = null;
      let timeoutId = null;
      let stableElement = null;
      let lastFingerprint = null;
      let stableSince = 0;
      let dirty = false;
      let lastTickAt = 0;

      function cleanup() {
        if (observer) observer.disconnect();
        if (intervalId !== null) clearInterval(intervalId);
        if (timeoutId !== null) clearTimeout(timeoutId);
        observer = null;
        intervalId = null;
        timeoutId = null;
      }

      function settle(fn, value) {
        if (settled) return;
        settled = true;
        cleanup();
        fn(value);
      }

      function resetStable() {
        stableElement = null;
        lastFingerprint = null;
      }

      // One evaluation of the page; returns the resolution value or undefined.
      function check() {
        const all = findAll();
        const element = all.length ? all[0] : null;
        if (!scope.checkElementConditions(element, options.state, options.conditions, all)) {
          resetStable();
          return undefined;
        }
        if (element === null) {
          return { element: null, matched: true, count: all.length };
        }
        if (options.needsStable) {
          const fp = fingerprint(element);
          if (element !== stableElement || fp !== lastFingerprint) {
            stableElement = element;
            lastFingerprint = fp;
            stableSince = Date.now();
            if (options.stableDuration > 0) return undefined;
          }
          if (Date.now() - stableSince < options.stableDuration) return undefined;
        }
        return scope.serializeElement(element);
      }

      // Every entry point (seed, poll, timeout) goes through here: an
      // exception rejects the wait instead of leaving it pending.
      function tick() {
        if (settled || ticking) return;
        ticking = true;
        dirty = false;
        lastTickAt = Date.now();
        try {
          const value = check();
          if (value !== undefined) settle(resolve, value);
        } catch (error) {
          settle(reject, toWaitError(error));
        } finally {
          ticking = false;
          // Mutations queued by our own evaluation (custom_js, serialization) must not
          // schedule another tick.
          if (observer) observer.takeRecords();
        }
      }

      function timedOut() {
        settle(
          reject,
          codedError(
            "wait_timeout",
            `waitForSelector timed out after ${options.timeoutMs} ms waiting for ` +
              `${options.selectorType} ${JSON.stringify(options.selector)} ` +
              `(url: ${location.href}, title: ${JSON.stringify(document.title)})`
          )
        );
      }

      // The observer only marks the page dirty; evaluation happens here, at most once per
      // interval, when dirty, when a stability window is due, or after IDLE_RECHECK_MS.
      function poll() {
        if (settled) return;
        const now = Date.now();
        const stableDue = stableElement !== null && now - stableSince >= options.stableDuration;
        if (dirty || stableDue || now - lastTickAt >= IDLE_RECHECK_MS) {
          tick();
        }
      }

      function finish() {
        if (settled) return;
        tick();
        if (!settled) timedOut();
      }

      tick();
      if (settled) return;
      if (options.timeoutMs === 0) {
        timedOut(); // 0 = single check
        return;
      }

      try {
        observer = new MutationObserver((records) => {
          if (ticking) return;
          if (Array.isArray(records) && records.length > 0 && records.every(isOwnRecord)) return;
          dirty = true;
        });
        observer.observe(document.documentElement || document, {
          childList: true,
          subtree: true,
          attributes: true,
          characterData: true,
        });
        intervalId = setInterval(poll, POLL_INTERVAL_MS);
        timeoutId = setTimeout(finish, options.timeoutMs);
      } catch (error) {
        settle(reject, toWaitError(error));
      }
    });
  }

  Object.assign(scope, { waitForSelector });
})();
