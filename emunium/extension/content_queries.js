(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;

  function querySelector({ selector }) {
    const element = document.querySelector(selector);
    if (!element) {
      return { error: "Element not found", selector };
    }
    return scope.serializeElement(element);
  }

  function querySelectorAll({ selector }) {
    return Array.from(document.querySelectorAll(selector)).map(scope.serializeElement);
  }

  function queryXPath({ xpath }) {
    const result = document.evaluate(
      xpath,
      document,
      null,
      XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
      null
    );
    const matches = [];
    for (let index = 0; index < result.snapshotLength; index += 1) {
      const element = result.snapshotItem(index);
      if (element?.nodeType === Node.ELEMENT_NODE) {
        matches.push(scope.serializeElement(element));
      }
    }
    return matches;
  }

  function queryByText({ text, exact }) {
    return scope.findElementsByText(text, Boolean(exact)).map(scope.serializeElement);
  }

  function getAllInteractive() {
    return Array.from(document.querySelectorAll(scope.INTERACTIVE_SELECTOR))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      })
      .map(scope.serializeElement);
  }

  Object.assign(scope, {
    getAllInteractive,
    queryByText,
    querySelector,
    querySelectorAll,
    queryXPath,
  });
})();
