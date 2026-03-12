(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;

  function scrollIntoView({ elementId }) {
    return scope.withResolvedElement(elementId, (element) => {
      const coords = scope.getAbsoluteCoords(element, { scroll: true });
      return {
        success: true,
        rect: scope.toRectPayload(coords.rect),
        absoluteScreenX: coords.absoluteScreenX,
        absoluteScreenY: coords.absoluteScreenY,
      };
    });
  }

  function scrollToPosition({ x, y }) {
    window.scrollTo(x, y);
    return { scrollX: window.scrollX, scrollY: window.scrollY };
  }

  function getPageInfo() {
    return {
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      screenX: window.screenX,
      screenY: window.screenY,
      outerWidth: window.outerWidth,
      outerHeight: window.outerHeight,
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
    };
  }

  function executeScript({ code }) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ __emunium_exec__: true, code }, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ error: chrome.runtime.lastError.message });
          return;
        }
        resolve(response || { error: "No result" });
      });
    });
  }

  function focusElement({ elementId }) {
    return scope.withResolvedElement(elementId, (element) => {
      element.focus();
      return { success: true };
    });
  }

  function getAttribute({ elementId, name }) {
    return scope.withResolvedElement(elementId, (element) => ({
      value: element.getAttribute(name),
    }));
  }

  function getComputedStyleProp({ elementId, property }) {
    return scope.withResolvedElement(elementId, (element) => ({
      value: window.getComputedStyle(element).getPropertyValue(property),
    }));
  }

  function getElementCoords({ elementId }) {
    return scope.withResolvedElement(elementId, (element) => {
      const coords = scope.getAbsoluteCoords(element, { scroll: true });
      return {
        absoluteScreenX: coords.absoluteScreenX,
        absoluteScreenY: coords.absoluteScreenY,
        rect: scope.toRectPayload(coords.rect),
      };
    });
  }

  const dispatch = {
    querySelector: scope.querySelector,
    querySelectorAll: scope.querySelectorAll,
    queryXPath: scope.queryXPath,
    queryByText: scope.queryByText,
    getAllInteractive: scope.getAllInteractive,
    scrollIntoView,
    scrollTo: scrollToPosition,
    pageInfo: getPageInfo,
    executeScript,
    waitForSelector: scope.waitForSelector,
    focus: focusElement,
    getAttribute,
    getComputedStyle: getComputedStyleProp,
    getElementCoords,
    ping: () => ({ pong: true, url: location.href }),
  };

  async function handleMessage(msg) {
    const { id, method, params } = msg;
    try {
      const handler = dispatch[method];
      const result = handler
        ? await handler(params || {})
        : { error: "Unknown method: " + method };
      return { id, result };
    } catch (error) {
      return { id, result: { error: error.message } };
    }
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.__emunium__) {
      handleMessage(msg).then((response) => sendResponse(response));
      return true;
    }
    return undefined;
  });

  chrome.runtime.sendMessage({ __emunium_ready__: true, url: location.href });
})();
