// content_main.js - MAIN-world evaluator for custom_js wait conditions.
//
// Not declared in the manifest: the worker injects it on demand with
// chrome.scripting.executeScript({files: ["content_main.js"], world: "MAIN"}) before a
// waitForSelector that uses custom_js. MV3 isolated worlds always refuse
// `new Function`; the page's MAIN world allows it unless the page CSP forbids eval.
//
// Channel (synchronous, nodes only - JS objects do not cross worlds, elements do):
//   1. The isolated world dispatches HELLO on `document` with its detached inbox as
//      relatedTarget. The armed listener answers once by dispatching READY on the inbox
//      with this script's detached port as relatedTarget, then removes itself, so no
//      listener stays on document/window after the handshake.
//   2. For each evaluation the isolated world appends a detached request node (attribute
//      `code`) to the port and dispatches EVAL on it (bubbles to the port, relatedTarget =
//      element or null). The port listener writes `status` (true|false|csp|syntax|async) and
//      `message` onto the request. No page element is ever modified.
(() => {
  "use strict";

  const MAIN_HELLO = "__emunium_main_hello";
  const MAIN_READY = "__emunium_main_ready";
  const MAIN_EVAL = "__emunium_main_eval";
  const COMPILE_CACHE_LIMIT = 64;

  // Captured at install so later page patches do not change the evaluator.
  const FunctionCtor = Function;
  const MouseEventCtor = MouseEvent;
  const PromiseCtor = Promise;
  const PromiseCatch = Promise.prototype.catch;
  const doc = document;

  function isDetachedElement(node) {
    return Boolean(node) && node.nodeType === 1 && !node.isConnected;
  }

  // Install once: if an earlier run of this file is still armed, it answers this probe
  // with its port and we re-arm with that same port instead of creating a second one.
  function findInstalledPort() {
    const probe = doc.createElement("div");
    let found = null;
    const onReady = (event) => {
      if (isDetachedElement(event.relatedTarget)) {
        found = event.relatedTarget;
      }
    };
    probe.addEventListener(MAIN_READY, onReady);
    try {
      doc.dispatchEvent(new MouseEventCtor(MAIN_HELLO, { relatedTarget: probe }));
    } finally {
      probe.removeEventListener(MAIN_READY, onReady);
    }
    return found;
  }

  function isCodegenBlocked(error) {
    const message = String((error && error.message) || error);
    return (
      (typeof EvalError !== "undefined" && error instanceof EvalError) ||
      (error && error.name === "EvalError") ||
      /Content Security Policy|unsafe-eval|TrustedScript|Trusted Type|Code generation from strings/i.test(message)
    );
  }

  function createPort() {
    const port = doc.createElement("div");
    const compiled = new Map();

    function compile(code) {
      const cached = compiled.get(code);
      if (cached) {
        return { fn: cached };
      }
      try {
        const fn = new FunctionCtor("el", code);
        if (compiled.size >= COMPILE_CACHE_LIMIT) {
          compiled.delete(compiled.keys().next().value);
        }
        compiled.set(code, fn);
        return { fn };
      } catch (error) {
        const message = String((error && error.message) || error);
        if (isCodegenBlocked(error)) {
          return { status: "csp", message };
        }
        return { status: "syntax", message };
      }
    }

    function onEval(event) {
      const request = event.target;
      if (!isDetachedElement(request) || request.parentNode !== port) {
        return;
      }
      const code = request.getAttribute("code");
      if (code === null) {
        return;
      }
      const element = event.relatedTarget && event.relatedTarget.nodeType === 1 ? event.relatedTarget : null;
      const outcome = compile(code);
      if (!outcome.fn) {
        request.setAttribute("status", outcome.status);
        request.setAttribute("message", outcome.message);
        return;
      }
      let result;
      try {
        result = outcome.fn.call(undefined, element);
      } catch {
        result = false; // runtime exceptions mean "not yet"
      }
      if (
        result !== null &&
        (typeof result === "object" || typeof result === "function") &&
        typeof result.then === "function"
      ) {
        if (result instanceof PromiseCtor) PromiseCatch.call(result, () => {});
        request.setAttribute("status", "async"); // custom_js must return synchronously
        return;
      }
      request.setAttribute("status", result ? "true" : "false");
    }

    port.addEventListener(MAIN_EVAL, onEval);
    return port;
  }

  function arm(port) {
    const onHello = (event) => {
      const inbox = event.relatedTarget;
      if (!isDetachedElement(inbox)) {
        return;
      }
      doc.removeEventListener(MAIN_HELLO, onHello);
      inbox.dispatchEvent(new MouseEventCtor(MAIN_READY, { relatedTarget: port }));
    };
    doc.addEventListener(MAIN_HELLO, onHello);
  }

  arm(findInstalledPort() || createPort());
})();
