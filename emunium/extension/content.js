(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;
  const PROTOCOL = 2;
  const DEFAULT_PROBE_WAIT_MS = 500;
  const DEFAULT_PROBE_QUIET_MS = 50;
  const MAX_PROBE_MS = 10000;

  // ── helpers ─────────────────────────────────────────────────────────────

  const FRAME_TAGS = new Set(["IFRAME", "FRAME", "EMBED", "OBJECT"]);

  // Wall clock, the same clock as Python's `since`. performance.timeOrigin drifts from
  // Date.now(), so samples are converted to wall time when they are recorded.
  function nowEpochMs() {
    return Date.now();
  }

  function eventWallTime(event) {
    const age = performance.now() - event.timeStamp;
    return Date.now() - (Number.isFinite(age) && age > 0 ? age : 0);
  }

  function isFrameElement(node) {
    return Boolean(node && node.nodeType === 1 && FRAME_TAGS.has(String(node.tagName).toUpperCase()));
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
  }

  function requireString(value, name) {
    if (typeof value !== "string" || value === "") {
      throw scope.codedError("invalid_argument", `'${name}' must be a non-empty string`);
    }
    return value;
  }

  function requireNumber(value, name) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw scope.codedError("invalid_argument", `'${name}' must be a finite number`);
    }
    return value;
  }

  function optionalDuration(value, name, fallback) {
    if (value == null) return fallback;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
      throw scope.codedError("invalid_argument", `'${name}' must be a number >= 0`);
    }
    return Math.min(value, MAX_PROBE_MS);
  }

  function shadowRootOf(element) {
    try {
      if (globalThis.chrome && chrome.dom && typeof chrome.dom.openOrClosedShadowRoot === "function") {
        const root = chrome.dom.openOrClosedShadowRoot(element);
        if (root) return root;
      }
    } catch {
      // not a host, or the API is unavailable in this context
    }
    return element.shadowRoot || null;
  }

  // `node` is `ancestor` or inside it in the flat tree: slotted nodes go to their slot,
  // shadow roots to their host.
  function composedContains(ancestor, node) {
    for (let current = node; current; ) {
      if (current === ancestor) return true;
      let parent = current.assignedSlot || current.parentNode || null;
      if (parent && parent.nodeType === 11 && parent.host) parent = parent.host;
      current = parent;
    }
    return false;
  }

  // Document-level hit test returns shadow hosts; descend to the innermost element.
  function deepElementFromPoint(x, y) {
    let hit = document.elementFromPoint(x, y);
    for (let depth = 0; hit && depth < 32; depth += 1) {
      const root = shadowRootOf(hit);
      if (!root || typeof root.elementFromPoint !== "function") break;
      const inner = root.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit || null;
  }

  function deepActiveElement() {
    let active = document.activeElement;
    for (let depth = 0; active && depth < 32; depth += 1) {
      const root = shadowRootOf(active);
      if (!root || !root.activeElement || root.activeElement === active) break;
      active = root.activeElement;
    }
    return active || null;
  }

  // ── pointer tracking ────────────────────────────────────────────────────

  const pointer = scope.pointer || { latest: null, waiters: new Set(), installed: false };
  scope.pointer = pointer;

  function storeSample(event, left, type) {
    const sample = {
      clientX: event.clientX,
      clientY: event.clientY,
      screenX: event.screenX,
      screenY: event.screenY,
      time: eventWallTime(event),
    };
    if (type) sample.type = type;
    if (left) sample.left = true;
    pointer.latest = sample;
    for (const wake of Array.from(pointer.waiters)) wake();
  }

  // Page-synthesised events say nothing about the OS cursor.
  function recordPointer(event) {
    if (event && event.isTrusted === true) storeSample(event, false);
  }

  // Leaving the document (window edge, or into a frame whose events this document never
  // sees) must supersede the last sample, which may lie inside the target element.
  function recordPointerLeave(event) {
    if (!event || event.isTrusted !== true) return;
    const related = event.relatedTarget;
    if (related == null || isFrameElement(related)) storeSample(event, true);
  }

  // During a native HTML5 drag Chrome sends only drag/dragover, no pointermove/mousemove:
  // dragover is the cursor position over this document.
  function recordDragOver(event) {
    if (event && event.isTrusted === true) storeSample(event, false, "dragover");
  }

  function installPointerTracking() {
    if (pointer.installed) return;
    pointer.installed = true;
    const options = { capture: true, passive: true };
    window.addEventListener("pointermove", recordPointer, options);
    window.addEventListener("mousemove", recordPointer, options);
    window.addEventListener("pointerout", recordPointerLeave, options);
    window.addEventListener("mouseout", recordPointerLeave, options);
    window.addEventListener("dragover", recordDragOver, options);
    window.addEventListener("dragleave", recordPointerLeave, options);
  }

  function freshSample(since) {
    const sample = pointer.latest;
    return sample && sample.time >= since ? sample : null;
  }

  function waitForPointer(ms) {
    return new Promise((resolve) => {
      let timer = null;
      const wake = () => {
        pointer.waiters.delete(wake);
        clearTimeout(timer);
        resolve();
      };
      pointer.waiters.add(wake);
      timer = setTimeout(wake, Math.max(0, ms));
    });
  }

  async function pointerProbe(params) {
    const { elementId } = params;
    scope.resolveElement(elementId);
    const since = requireNumber(params.since, "since");
    const waitMs = optionalDuration(params.waitMs, "waitMs", DEFAULT_PROBE_WAIT_MS);
    const quietMs = optionalDuration(params.quietMs, "quietMs", DEFAULT_PROBE_QUIET_MS);

    const start = nowEpochMs();
    const deadline = start + waitMs;
    let sample = freshSample(since);
    while (!sample && nowEpochMs() < deadline) {
      await waitForPointer(deadline - nowEpochMs());
      sample = freshSample(since);
    }
    if (sample) {
      // The cursor may still be settling: wait for quietMs without a newer sample,
      // bounded so a constantly moving cursor cannot hold the probe open.
      const cap = deadline + quietMs;
      for (;;) {
        sample = pointer.latest;
        const now = nowEpochMs();
        const quietAt = sample.time + quietMs;
        if (now >= quietAt || now >= cap) break;
        await sleep(Math.min(quietAt, cap) - now);
      }
    }

    const element = scope.resolveElement(elementId);
    const rect = scope.toRectPayload(element.getBoundingClientRect());
    const geometry = scope.readGeometry();
    if (!sample) {
      return { event: null, hit: false, rect, geometry };
    }
    const event = {
      clientX: sample.clientX,
      clientY: sample.clientY,
      screenX: sample.screenX,
      screenY: sample.screenY,
      time: sample.time,
    };
    if (sample.type) event.type = sample.type;
    if (sample.left) event.left = true;
    return { event, hit: !sample.left && isHit(element, sample.clientX, sample.clientY), rect, geometry };
  }

  // A frame at the point owns the pointer: its content gets the events, not `element`
  // (unless the target is that frame).
  function isHit(element, x, y) {
    const hitElement = deepElementFromPoint(x, y);
    if (hitElement === null) return false;
    if (isFrameElement(hitElement) && hitElement !== element) return false;
    return composedContains(element, hitElement);
  }

  // ── methods ─────────────────────────────────────────────────────────────

  function pageInfo() {
    return {
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      // documentElement is null in documents that are still being created or were cleared.
      scrollWidth: document.documentElement?.scrollWidth ?? 0,
      scrollHeight: document.documentElement?.scrollHeight ?? 0,
      geometry: scope.readGeometry(),
    };
  }

  function describeElement({ elementId }) {
    return scope.serializeElement(scope.resolveElement(elementId));
  }

  function scrollIntoView({ elementId }) {
    const element = scope.resolveElement(elementId);
    element.scrollIntoView({ behavior: "instant", block: "center" });
    return scope.serializeElement(element);
  }

  function scrollToPosition({ x, y }) {
    const left = requireNumber(x, "x");
    const top = requireNumber(y, "y");
    window.scrollTo({ left, top, behavior: "instant" });
    return { scrollX: window.scrollX, scrollY: window.scrollY };
  }

  function focusElement({ elementId }) {
    const element = scope.resolveElement(elementId);
    element.focus();
    const active = deepActiveElement();
    return { focused: active !== null && composedContains(element, active) };
  }

  function focusState({ elementId }) {
    const element = scope.resolveElement(elementId);
    const active = deepActiveElement();
    return {
      documentHasFocus: document.hasFocus(),
      activeIsElement: active !== null && composedContains(element, active),
      visibilityState: document.visibilityState,
    };
  }

  function getAttribute({ elementId, name }) {
    const element = scope.resolveElement(elementId);
    return { value: element.getAttribute(requireString(name, "name")) };
  }

  function getComputedStyleProp({ elementId, property }) {
    const element = scope.resolveElement(elementId);
    return { value: window.getComputedStyle(element).getPropertyValue(requireString(property, "property")) };
  }

  // Scope members are looked up at call time: a re-injection replaces them.
  const METHODS = {
    ping: () => ({ pong: true, url: location.href }),
    pageInfo,
    querySelector: (params) => scope.querySelector(params),
    querySelectorAll: (params) => scope.querySelectorAll(params),
    queryXPath: (params) => scope.queryXPath(params),
    queryByText: (params) => scope.queryByText(params),
    getAllInteractive: (params) => scope.getAllInteractive(params),
    describeElement,
    scrollIntoView,
    scrollTo: scrollToPosition,
    focus: focusElement,
    focusState,
    pointerProbe,
    getAttribute,
    getComputedStyle: getComputedStyleProp,
    waitForSelector: (params) => scope.waitForSelector(params),
  };

  function errorPayload(error) {
    if (error && typeof error.code === "string" && error.code) {
      return { code: error.code, message: String(error.message || "") };
    }
    const message = String((error && error.message) || error);
    if (error && error.name === "SyntaxError") {
      return { code: "invalid_argument", message };
    }
    return { code: "internal", message };
  }

  async function handleMessage(msg) {
    const method = msg.method;
    const params = msg.params && typeof msg.params === "object" ? msg.params : {};
    try {
      if (typeof method !== "string" || !Object.prototype.hasOwnProperty.call(METHODS, method)) {
        throw scope.codedError("unknown_method", `unknown content method '${method}'`);
      }
      const result = await METHODS[method](params);
      return { ok: true, result: result === undefined ? null : result };
    } catch (error) {
      return { ok: false, error: errorPayload(error) };
    }
  }

  function onMessage(msg, sender, sendResponse) {
    if (!msg || typeof msg !== "object" || msg.__emunium__ !== PROTOCOL) {
      return undefined;
    }
    scope.handleMessage(msg).then(sendResponse);
    return true;
  }

  function isTopFrame() {
    try {
      return window.top === window;
    } catch {
      return false;
    }
  }

  function announceReady() {
    try {
      const sent = chrome.runtime.sendMessage({ __emunium_ready__: PROTOCOL, url: location.href });
      if (sent && typeof sent.catch === "function") sent.catch(() => {});
    } catch {
      // extension context invalidated (reloaded extension): nobody to tell
    }
  }

  // Re-injection replaces the handlers but never adds a second listener.
  scope.handleMessage = handleMessage;
  scope.contentMethods = METHODS;
  installPointerTracking();
  if (!scope.listenerInstalled) {
    scope.listenerInstalled = true;
    chrome.runtime.onMessage.addListener(onMessage);
  }
  if (!scope.readySent && isTopFrame()) {
    scope.readySent = true;
    announceReady();
  }
})();
