(() => {
  "use strict";

  const scope = (globalThis.EmuniumContent = globalThis.EmuniumContent || {});
  const state = scope.state || { elements: new Map(), nextId: 1 };
  const INTERACTIVE_SELECTOR =
    "input,button,a,textarea,select,[role],[aria-label],[data-state]," +
    "[placeholder],[data-testid],[name],[type],[contenteditable]";

  function nextElementId() {
    const id = "e_" + state.nextId;
    state.nextId += 1;
    return id;
  }

  function getElementId(element) {
    for (const [id, reference] of state.elements) {
      if (reference === element) {
        return id;
      }
    }
    const id = nextElementId();
    state.elements.set(id, element);
    return id;
  }

  function resolveElement(elementId) {
    const element = state.elements.get(elementId);
    if (!element || !element.isConnected) {
      state.elements.delete(elementId);
      return null;
    }
    return element;
  }

  function getWindowBorders() {
    return {
      left: (window.outerWidth - window.innerWidth) / 2,
      top: window.outerHeight - window.innerHeight,
    };
  }

  function toRectPayload(rect) {
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }

  function getElementText(element, maxLength = 500) {
    return (element.innerText || element.textContent || "").slice(0, maxLength);
  }

  function getAbsoluteCoords(element, { scroll = false } = {}) {
    if (scroll) {
      element.scrollIntoView({ behavior: "instant", block: "center" });
    }
    const rect = element.getBoundingClientRect();
    const borders = getWindowBorders();
    return {
      absoluteScreenX: window.screenX + borders.left + rect.x + rect.width / 2,
      absoluteScreenY: window.screenY + borders.top + rect.y + rect.height / 2,
      rect,
    };
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
      rect: toRectPayload(coords.rect),
      text: getElementText(element),
      visible: coords.rect.width > 0 && coords.rect.height > 0,
      value: element.value !== undefined ? element.value : null,
      absoluteScreenX: coords.absoluteScreenX,
      absoluteScreenY: coords.absoluteScreenY,
    };
  }

  function withResolvedElement(elementId, callback) {
    const element = resolveElement(elementId);
    if (!element) {
      return { error: "Element not found or detached" };
    }
    return callback(element);
  }

  function findElementsByText(text, exact = false) {
    if (!document.body) {
      return [];
    }
    const matcher = exact ? text : text.toLowerCase();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, null);
    const matches = [];
    let node;
    while ((node = walker.nextNode())) {
      const value = (node.innerText || node.textContent || "").trim();
      if (!value) {
        continue;
      }
      if (exact ? value === text : value.toLowerCase().includes(matcher)) {
        matches.push(node);
      }
    }
    return matches;
  }

  Object.assign(scope, {
    INTERACTIVE_SELECTOR,
    findElementsByText,
    getAbsoluteCoords,
    getElementId,
    getElementText,
    resolveElement,
    serializeElement,
    state,
    toRectPayload,
    withResolvedElement,
  });
})();
