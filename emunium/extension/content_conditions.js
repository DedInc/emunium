(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;

  // Event types shared with content_main.js (MAIN world). Elements cross worlds, JS
  // objects do not, so every exchange carries nodes (relatedTarget) and attributes on
  // detached nodes only.
  const MAIN_HELLO = "__emunium_main_hello";
  const MAIN_READY = "__emunium_main_ready";
  const MAIN_EVAL = "__emunium_main_eval";

  const MAX_DEPTH = 32;
  const COMPILE_CACHE_LIMIT = 64;

  const LEAF_TYPES = new Set([
    "visible",
    "hidden",
    "detached",
    "clickable",
    "unobscured",
    "text_not_empty",
    "text_contains",
    "has_attribute",
    "without_attribute",
    "has_class",
    "has_style",
    "custom_js",
    "stable",
    "count_gt",
    "count_eq",
  ]);
  const GROUP_TYPES = new Set(["any_of", "all_of"]);

  // Nodes created by emunium (channel nodes); the wait observer ignores their records.
  const ownNodes = scope.ownNodes || new WeakSet();
  // Survives re-injection into the same isolated world: the MAIN handshake is one-shot.
  const channel = scope.mainChannel || { port: null, inbox: null, isolatedBlocked: false };
  const isolatedCompiled = new Map();

  function codedError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
  }

  function isElement(node) {
    return Boolean(node) && node.nodeType === 1;
  }

  // ── validation ──────────────────────────────────────────────────────────

  function describe(path) {
    return path || "conditions";
  }

  function requireString(value, path, field) {
    if (typeof value !== "string") {
      throw codedError("invalid_argument", `${describe(path)}.${field} must be a string`);
    }
  }

  function requireCount(value, path) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw codedError("invalid_argument", `${describe(path)}.value must be a finite number`);
    }
  }

  function validateLeaf(condition, path) {
    switch (condition.type) {
      case "text_contains":
      case "has_class":
        requireString(condition.value, path, "value");
        break;
      case "has_attribute":
        requireString(condition.name, path, "name");
        if (condition.value != null) requireString(condition.value, path, "value");
        break;
      case "without_attribute":
        requireString(condition.name, path, "name");
        break;
      case "has_style":
        requireString(condition.name, path, "name");
        requireString(condition.value, path, "value");
        break;
      case "custom_js":
        requireString(condition.code, path, "code");
        break;
      case "count_gt":
      case "count_eq":
        requireCount(condition.value, path);
        break;
      case "stable":
        if (
          condition.duration != null &&
          (typeof condition.duration !== "number" ||
            !Number.isFinite(condition.duration) ||
            condition.duration < 0)
        ) {
          throw codedError("invalid_argument", `${describe(path)}.duration must be a number >= 0`);
        }
        break;
      default:
        break;
    }
  }

  function validateList(list, path, depth) {
    if (!Array.isArray(list)) {
      throw codedError("invalid_argument", `${describe(path)} must be an array of conditions`);
    }
    if (depth > MAX_DEPTH) {
      throw codedError("invalid_argument", `conditions nested deeper than ${MAX_DEPTH} levels`);
    }
    list.forEach((condition, index) => {
      const here = `${describe(path)}[${index}]`;
      if (!condition || typeof condition !== "object" || Array.isArray(condition)) {
        throw codedError("invalid_argument", `${here} must be an object`);
      }
      const type = condition.type;
      if (GROUP_TYPES.has(type)) {
        if (!Array.isArray(condition.conditions)) {
          throw codedError("invalid_argument", `${here}.conditions must be an array of groups`);
        }
        condition.conditions.forEach((group, groupIndex) =>
          validateList(group, `${here}.conditions[${groupIndex}]`, depth + 1)
        );
        return;
      }
      if (type === "not") {
        validateList(condition.condition, `${here}.condition`, depth + 1);
        return;
      }
      if (typeof type !== "string" || !LEAF_TYPES.has(type)) {
        throw codedError("unknown_condition", `unknown condition type ${JSON.stringify(type)} at ${here}`);
      }
      validateLeaf(condition, here);
    });
  }

  /** Throws `unknown_condition` / `invalid_argument`; returns the list ([] for null). */
  function validateConditions(conditions) {
    if (conditions == null) {
      return [];
    }
    validateList(conditions, "conditions", 0);
    return conditions;
  }

  // Calls `visit` on every leaf condition, recursing through any_of / all_of / not.
  function walkLeaves(conditions, visit) {
    if (!Array.isArray(conditions)) return;
    for (const condition of conditions) {
      if (!condition) continue;
      if (GROUP_TYPES.has(condition.type)) {
        for (const group of condition.conditions || []) walkLeaves(group, visit);
      } else if (condition.type === "not") {
        walkLeaves(condition.condition, visit);
      } else {
        visit(condition);
      }
    }
  }

  function hasConditionType(conditions, type) {
    let found = false;
    walkLeaves(conditions, (c) => {
      if (c.type === type) found = true;
    });
    return found;
  }

  function hasDetachedCondition(conditions) {
    return hasConditionType(conditions, "detached");
  }

  // ── element predicates ──────────────────────────────────────────────────

  function isVisible(element) {
    if (!isElement(element) || !element.isConnected) {
      return false;
    }
    // offsetWidth/offsetHeight exist only on HTMLElement (undefined for SVG and others).
    if (typeof element.checkVisibility === "function") {
      if (!element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) {
        return false;
      }
    } else {
      if (typeof element.getClientRects === "function" && element.getClientRects().length === 0) {
        return false; // display:none (self or ancestor) or not rendered
      }
      const style = getComputedStyle(element);
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        style.visibility === "collapse" ||
        parseFloat(style.opacity) === 0
      ) {
        return false;
      }
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }

  function isClickable(element) {
    if (!isVisible(element) || element.disabled) {
      return false;
    }
    return getComputedStyle(element).pointerEvents !== "none";
  }

  function isUnobscured(element) {
    if (!isElement(element) || !element.isConnected) {
      return false;
    }
    const rect = element.getBoundingClientRect();
    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;
    const topElement = document.elementFromPoint(centerX, centerY);
    return topElement !== null && (topElement === element || element.contains(topElement));
  }

  function elementText(el) {
    return el.innerText || el.textContent || "";
  }

  // ── custom_js ───────────────────────────────────────────────────────────

  function isCodegenBlocked(error) {
    const message = String((error && error.message) || error);
    return (
      (typeof EvalError !== "undefined" && error instanceof EvalError) ||
      (error && error.name === "EvalError") ||
      /Content Security Policy|unsafe-eval|TrustedScript|Trusted Type|Code generation from strings/i.test(message)
    );
  }

  function rememberCompiled(code, entry) {
    if (isolatedCompiled.size >= COMPILE_CACHE_LIMIT) {
      isolatedCompiled.delete(isolatedCompiled.keys().next().value);
    }
    isolatedCompiled.set(code, entry);
  }

  // Returns a function, or null when the isolated world forbids code generation
  // (always the case in MV3 isolated worlds). Tried once per world: a blocked attempt
  // is not repeated on every tick.
  function compileIsolated(code) {
    if (channel.isolatedBlocked) {
      return null;
    }
    const cached = isolatedCompiled.get(code);
    if (cached) {
      return cached;
    }
    let fn;
    try {
      fn = new Function("el", code);
    } catch (error) {
      if (isCodegenBlocked(error)) {
        channel.isolatedBlocked = true;
        return null;
      }
      if (error && error.name === "SyntaxError") {
        throw codedError("script_error", `custom_js: ${error.message}`);
      }
      throw codedError("script_error", `custom_js could not be compiled: ${String((error && error.message) || error)}`);
    }
    rememberCompiled(code, fn);
    return fn;
  }

  function isChannelNode(node) {
    return isElement(node) && !node.isConnected;
  }

  // Synchronous handshake: the MAIN script answers a hello dispatched with our detached
  // inbox as relatedTarget by dispatching "ready" on the inbox with its detached port.
  function connectMainWorld() {
    if (isChannelNode(channel.port)) {
      return channel.port;
    }
    channel.port = null;
    const inbox = channel.inbox || document.createElement("div");
    channel.inbox = inbox;
    ownNodes.add(inbox);
    let port = null;
    const onReady = (event) => {
      if (isChannelNode(event.relatedTarget)) {
        port = event.relatedTarget;
      }
    };
    inbox.addEventListener(MAIN_READY, onReady);
    try {
      document.dispatchEvent(new MouseEvent(MAIN_HELLO, { relatedTarget: inbox }));
    } finally {
      inbox.removeEventListener(MAIN_READY, onReady);
    }
    channel.port = port;
    return port;
  }

  function evaluateInMainWorld(el, code) {
    const port = connectMainWorld();
    if (!port) {
      throw codedError("content_unavailable", "custom_js needs the MAIN-world evaluator (content_main.js), which is not installed");
    }
    const request = document.createElement("div");
    ownNodes.add(request);
    request.setAttribute("code", code);
    port.appendChild(request);
    try {
      // Bubbles from the request to the port's listener; relatedTarget carries the element.
      request.dispatchEvent(new MouseEvent(MAIN_EVAL, { bubbles: true, relatedTarget: el || null }));
    } finally {
      request.remove();
    }
    const status = request.getAttribute("status");
    const message = request.getAttribute("message") || "";
    switch (status) {
      case "true":
        return true;
      case "false":
        return false;
      case "csp":
        throw codedError("csp_blocked", `page Content Security Policy blocks custom_js evaluation: ${message}`);
      case "syntax":
        throw codedError("script_error", `custom_js: ${message}`);
      case "async":
        throw asyncResultError();
      default:
        channel.port = null; // the evaluator went away; reconnect on the next call
        throw codedError("content_unavailable", "the MAIN-world evaluator did not answer");
    }
  }

  function runCustomJs(el, code) {
    const fn = compileIsolated(code);
    if (!fn) {
      return evaluateInMainWorld(el, code);
    }
    let result;
    try {
      result = fn.call(undefined, el);
    } catch {
      return false; // runtime exceptions mean "not yet"
    }
    if (isThenable(result)) {
      if (result instanceof Promise) result.catch(() => {}); // no unhandled rejection noise
      throw asyncResultError();
    }
    return Boolean(result);
  }

  function isThenable(value) {
    return (
      value !== null &&
      (typeof value === "object" || typeof value === "function") &&
      typeof value.then === "function"
    );
  }

  function asyncResultError() {
    return codedError("script_error", "custom_js must return synchronously (got a Promise/thenable)");
  }

  // ── evaluators (null-safe) ──────────────────────────────────────────────

  // Element-specific evaluators: false when there is no element.
  const ElementEvaluators = {
    visible: (el) => isVisible(el),
    clickable: (el) => isClickable(el),
    unobscured: (el) => isUnobscured(el),
    text_not_empty: (el) => Boolean(elementText(el).trim()),
    text_contains: (el, cond) => elementText(el).includes(cond.value),
    has_attribute: (el, cond) =>
      el.hasAttribute(cond.name) && (cond.value == null || el.getAttribute(cond.name) === cond.value),
    without_attribute: (el, cond) => !el.hasAttribute(cond.name),
    has_class: (el, cond) => el.classList.contains(cond.value),
    has_style: (el, cond) => window.getComputedStyle(el).getPropertyValue(cond.name) === cond.value,
    // Stability itself is measured by the wait tracker; as a predicate it needs an element.
    stable: () => true,
  };

  // Evaluators that are meaningful without an element.
  const AnyEvaluators = {
    hidden: (el) => !isElement(el) || !isVisible(el),
    detached: (el) => !isElement(el) || !el.isConnected,
    count_gt: (el, cond, all) => all.length > cond.value,
    count_eq: (el, cond, all) => all.length === cond.value,
    custom_js: (el, cond) => runCustomJs(isElement(el) ? el : null, cond.code),
  };

  function evaluate(el, condition, allElements) {
    const all = Array.isArray(allElements) ? allElements : [];
    const element = isElement(el) ? el : null;
    const type = condition && condition.type;
    if (type === "any_of") {
      return condition.conditions.some((group) => group.every((c) => evaluate(element, c, all)));
    }
    if (type === "all_of") {
      return condition.conditions.every((group) => group.every((c) => evaluate(element, c, all)));
    }
    if (type === "not") {
      // With no element, element-specific conditions are false, so not(...) of them holds:
      // e.g. not(has_class "x") is satisfied by a missing element and the wait can resolve
      // the element-less match {element: null, matched: true, count: 0}.
      return !condition.condition.every((c) => evaluate(element, c, all));
    }
    if (Object.prototype.hasOwnProperty.call(AnyEvaluators, type)) {
      return AnyEvaluators[type](element, condition, all);
    }
    if (Object.prototype.hasOwnProperty.call(ElementEvaluators, type)) {
      return element !== null && ElementEvaluators[type](element, condition, all);
    }
    throw codedError("unknown_condition", `unknown condition type ${JSON.stringify(type)}`);
  }

  function matchesCondition(el, condition, allElements) {
    return evaluate(el, condition, allElements);
  }

  // `state` presence (or unset) can hold without an element; every other state needs one.
  function stateNeedsElement(state) {
    return state != null && state !== "presence";
  }

  function checkElementConditions(element, state, conditions, allElements) {
    const all = Array.isArray(allElements) ? allElements : [];
    const list = Array.isArray(conditions) ? conditions : [];
    if (!isElement(element)) {
      // No match: succeed only when there is something to satisfy and all of it holds for null.
      if (stateNeedsElement(state) || list.length === 0) {
        return false;
      }
      return list.every((c) => evaluate(null, c, all));
    }
    if (state === "visible" && !isVisible(element)) {
      return false;
    }
    if (state === "clickable" && !isClickable(element)) {
      return false;
    }
    if (state === "unobscured" && (!isVisible(element) || !isUnobscured(element))) {
      return false;
    }
    return list.every((c) => evaluate(element, c, all));
  }

  Object.assign(scope, {
    checkElementConditions,
    evaluate,
    hasConditionType,
    hasDetachedCondition,
    isClickable,
    isUnobscured,
    isVisible,
    mainChannel: channel,
    matchesCondition,
    ownNodes,
    validateConditions,
    walkLeaves,
  });
})();
