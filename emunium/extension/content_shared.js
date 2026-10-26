(() => {
  "use strict";

  const scope = (globalThis.EmuniumContent = globalThis.EmuniumContent || {});

  const INTERACTIVE_SELECTOR =
    "input,button,a,textarea,select,[role],[aria-label],[data-state]," +
    "[placeholder],[data-testid],[name],[type],[contenteditable]";
  // Subtrees whose text is never rendered: they are neither text matches nor do they
  // hide a matching parent.
  const TEXT_SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);
  // Without FinalizationRegistry the id map is swept of collected entries this often.
  const SWEEP_EVERY = 1000;
  const LARGE_TEXT_LENGTH = 5000;
  // elementText cap (dx-api.md section 7): 1 MiB of chars keeps the reply far below
  // the 64 MiB frame limit even at 4 UTF-8 bytes per char plus JSON escaping.
  const FULL_TEXT_CAP = 1048576;

  function codedError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
  }

  function randomToken() {
    const bytes = new Uint8Array(6);
    if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === "function") {
      globalThis.crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  // One state per isolated world (= per document). Re-injecting the scripts into the
  // same document keeps it, so handles issued before the re-injection stay valid.
  function createState() {
    const refs = new Map();
    const registry =
      typeof FinalizationRegistry === "function" ? new FinalizationRegistry((id) => refs.delete(id)) : null;
    return { token: randomToken(), nextId: 1, refs, ids: new WeakMap(), registry, issuedSinceSweep: 0 };
  }

  const existing = scope.state;
  const state =
    existing && typeof existing.token === "string" && existing.refs instanceof Map && existing.ids instanceof WeakMap
      ? existing
      : createState();

  function sweep() {
    for (const [id, ref] of state.refs) {
      if (ref.deref() === undefined) state.refs.delete(id);
    }
  }

  function getElementId(element) {
    const known = state.ids.get(element);
    if (known !== undefined && state.refs.has(known)) {
      return known;
    }
    const id = state.token + "." + state.nextId;
    state.nextId += 1;
    state.refs.set(id, new WeakRef(element));
    state.ids.set(element, id);
    if (state.registry) {
      state.registry.register(element, id);
    } else if ((state.issuedSinceSweep += 1) >= SWEEP_EVERY) {
      state.issuedSinceSweep = 0;
      sweep();
    }
    return id;
  }

  /** Returns the live element or throws `stale_element` / `invalid_argument`. */
  function resolveElement(elementId) {
    if (typeof elementId !== "string" || elementId === "") {
      throw codedError("invalid_argument", "'elementId' must be a non-empty string");
    }
    const dot = elementId.lastIndexOf(".");
    if (dot <= 0 || elementId.slice(0, dot) !== state.token) {
      throw codedError("stale_element", `element ${elementId} belongs to another document`);
    }
    const ref = state.refs.get(elementId);
    const element = ref ? ref.deref() : undefined;
    if (element === undefined) {
      state.refs.delete(elementId);
      throw codedError("stale_element", `element ${elementId} no longer exists`);
    }
    if (!element.isConnected || element.ownerDocument !== document) {
      throw codedError("stale_element", `element ${elementId} is detached from the document`);
    }
    return element;
  }

  function withResolvedElement(elementId, callback) {
    return callback(resolveElement(elementId));
  }

  function toRectPayload(rect) {
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }

  // Window geometry at call time: screenX/Y and outerWidth/Height are DIP,
  // innerWidth/Height CSS px, devicePixelRatio = OS scale x page zoom.
  // scrollWidth/Height: extent of the scrolling element; clientWidth/Height: the viewport
  // without scrollbars, so the gap to innerWidth/Height is the scrollbar band. Both are 0
  // while the document has no root element.
  function readGeometry() {
    const viewport = window.visualViewport;
    const scroller = document.scrollingElement ?? null;
    const root = document.documentElement ?? null;
    return {
      scrollWidth: scroller?.scrollWidth ?? 0,
      scrollHeight: scroller?.scrollHeight ?? 0,
      clientWidth: root?.clientWidth ?? 0,
      clientHeight: root?.clientHeight ?? 0,
      screenX: window.screenX ?? 0,
      screenY: window.screenY ?? 0,
      outerWidth: window.outerWidth ?? 0,
      outerHeight: window.outerHeight ?? 0,
      innerWidth: window.innerWidth ?? 0,
      innerHeight: window.innerHeight ?? 0,
      devicePixelRatio: window.devicePixelRatio ?? 1,
      visualViewport: {
        offsetLeft: viewport?.offsetLeft ?? 0,
        offsetTop: viewport?.offsetTop ?? 0,
        scale: viewport?.scale ?? 1,
      },
    };
  }

  // Legacy DIP estimate of the rect centre, assuming page zoom 1 (the content script
  // cannot see the zoom; Python converts `rect` + `geometry` + reply `zoom` instead).
  function estimateScreenCenter(rect, geometry) {
    const border = Math.max(0, (geometry.outerWidth - geometry.innerWidth) / 2);
    const originX = geometry.screenX + border;
    const originY = geometry.screenY + geometry.outerHeight - geometry.innerHeight - border;
    return {
      absoluteScreenX: originX + rect.x + rect.width / 2,
      absoluteScreenY: originY + rect.y + rect.height / 2,
    };
  }

  function getAbsoluteCoords(element) {
    const rect = element.getBoundingClientRect();
    const geometry = readGeometry();
    return Object.assign({ rect, geometry }, estimateScreenCenter(rect, geometry));
  }

  // innerText lays out and builds the whole rendered text; for big containers the raw
  // textContent prefix is good enough for a 500-character preview.
  function getElementText(element, maxLength = 500) {
    const raw = element.textContent || "";
    if (raw.length > LARGE_TEXT_LENGTH) {
      return raw.slice(0, maxLength);
    }
    return (element.innerText || raw).slice(0, maxLength);
  }

  /** Full rendered text: innerText, else textContent, cut at FULL_TEXT_CAP chars. */
  function getFullElementText(element) {
    const rendered = element.innerText;
    const full = typeof rendered === "string" && rendered ? rendered : element.textContent || "";
    const truncated = full.length > FULL_TEXT_CAP;
    return { text: truncated ? full.slice(0, FULL_TEXT_CAP) : full, length: full.length, truncated };
  }

  function collectAttributes(element) {
    const attrs = {};
    for (const attr of element.attributes || []) {
      attrs[attr.name] = attr.value;
    }
    return attrs;
  }

  function serializeElement(element) {
    const coords = getAbsoluteCoords(element);
    return {
      elementId: getElementId(element),
      tag: element.tagName.toLowerCase(),
      attrs: collectAttributes(element),
      text: getElementText(element),
      value: element.value !== undefined ? element.value : null,
      visible: coords.rect.width > 0 && coords.rect.height > 0,
      rect: toRectPayload(coords.rect),
      geometry: coords.geometry,
      absoluteScreenX: coords.absoluteScreenX,
      absoluteScreenY: coords.absoluteScreenY,
    };
  }

  function collapseText(value) {
    return value.replace(/\s+/g, " ").trim().toLowerCase();
  }

  function isTextSkipped(element) {
    return TEXT_SKIP_TAGS.has(String(element.tagName).toUpperCase());
  }

  /** Deepest elements whose text matches: a match with a matching element child loses. */
  function findElementsByText(text, exact = false) {
    const root = document.body;
    if (!root || typeof text !== "string") {
      return [];
    }
    const needle = exact ? text : text.toLowerCase();
    // Pruning key: textContent is a cheap superset of the rendered text. Case is folded
    // even for exact matches because innerText applies CSS text-transform.
    const pruneNeedle = collapseText(text);
    const mayContain = (node) => collapseText(node.textContent || "").includes(pruneNeedle);
    const matched = new Set();
    const order = [];
    const visit = (node) => {
      const value = (node.innerText || node.textContent || "").trim();
      if (value && (exact ? value === needle : value.toLowerCase().includes(needle))) {
        matched.add(node);
        order.push(node);
      }
    };
    if (!mayContain(root)) {
      return [];
    }
    visit(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {
      acceptNode: (node) =>
        isTextSkipped(node) || !mayContain(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    let node;
    while ((node = walker.nextNode())) {
      visit(node);
    }
    return order.filter((candidate) => !Array.from(candidate.children || []).some((child) => matched.has(child)));
  }

  Object.assign(scope, {
    INTERACTIVE_SELECTOR,
    codedError,
    findElementsByText,
    getAbsoluteCoords,
    getElementId,
    getElementText,
    getFullElementText,
    readGeometry,
    resolveElement,
    serializeElement,
    state,
    toRectPayload,
    withResolvedElement,
  });
})();
