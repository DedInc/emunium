(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;

  function requireString(value, name) {
    if (typeof value !== "string" || value === "") {
      throw scope.codedError("invalid_argument", `'${name}' must be a non-empty string`);
    }
    return value;
  }

  // SyntaxError (bad CSS) passes through: handleMessage maps it to invalid_argument.
  function querySelector({ selector }) {
    const element = document.querySelector(requireString(selector, "selector"));
    return element ? scope.serializeElement(element) : null;
  }

  function querySelectorAll({ selector }) {
    return Array.from(document.querySelectorAll(requireString(selector, "selector")), (element) =>
      scope.serializeElement(element)
    );
  }

  function queryXPath({ xpath }) {
    requireString(xpath, "xpath");
    let result;
    try {
      result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    } catch (error) {
      // SyntaxError / NamespaceError: malformed; TypeError: not a node-set (e.g. count(//a)).
      const name = error && error.name;
      if (name === "SyntaxError" || name === "NamespaceError" || name === "TypeError") {
        throw scope.codedError("invalid_argument", `invalid XPath: ${error.message}`);
      }
      throw error;
    }
    const matches = [];
    for (let index = 0; index < result.snapshotLength; index += 1) {
      const element = result.snapshotItem(index);
      if (element && element.nodeType === Node.ELEMENT_NODE) {
        matches.push(scope.serializeElement(element));
      }
    }
    return matches;
  }

  function queryByText({ text, exact }) {
    requireString(text, "text");
    return scope.findElementsByText(text, Boolean(exact)).map((element) => scope.serializeElement(element));
  }

  function getAllInteractive() {
    return Array.from(document.querySelectorAll(scope.INTERACTIVE_SELECTOR))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      })
      .map((element) => scope.serializeElement(element));
  }

  Object.assign(scope, {
    getAllInteractive,
    queryByText,
    querySelector,
    querySelectorAll,
    queryXPath,
  });
})();
