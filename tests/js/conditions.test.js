"use strict";
// tests/js/conditions.test.js - content_conditions.js and content_wait.js: validation,
// null-safe evaluators, element-less matches, nested stable, tick errors, timeouts,
// custom_js (isolated world, and MAIN world through the worker's per-wait channel
// evaluator) and the manifest (docs/architecture/bridge-protocol.md sections 6-7).

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const h = require("./harness");

const BLOCK_EVAL =
  "Function = function () { throw new EvalError(\"Code generation from strings disallowed for this context\"); };";

// Isolated world: content_conditions.js + content_wait.js with S5 functions stubbed.
function makeIsolated(t, { document, blockEval = false, url = "https://example.test/page", title = "Page" } = {}) {
  const doc = document || h.createDocument({ url, title });
  const win = h.createWindow(doc, { url });
  const serialized = [];
  win.EmuniumContent = {
    findElementsByText: () => [],
    serializeElement: (el) => {
      serialized.push(el);
      return { tag: el.tagName.toLowerCase(), id: el.id || null };
    },
  };
  const ctx = h.loadScripts(["content_conditions.js", "content_wait.js"], win);
  if (blockEval) {
    ctx.__harness.evaluate(BLOCK_EVAL);
  }
  t.after(() => ctx.__harness.dispose());
  return { ctx, win, doc, scope: ctx.EmuniumContent, serialized };
}

// The worker's MAIN-world evaluator and its tree walk (background_handlers.js).
const worker = h.loadScripts(["background_handlers.js"], {
  EmuniumBackground: { codedError: (code, message) => Object.assign(new Error(message), { code }), errorMessage: String, timing: {} },
}).EmuniumBackground;

// MAIN world: its own context over the same document; `install` runs the evaluator the way
// chrome.scripting does (serialized func + args) and returns its result as a plain object.
function mainWorld(t, doc, { csp = false } = {}) {
  const isolatedView = doc.defaultView;
  const win = h.createWindow(doc);
  doc.defaultView = isolatedView;
  if (csp) {
    win.Function = function () {
      throw new EvalError("Refused to evaluate a string as JavaScript because 'unsafe-eval' is not allowed");
    };
  }
  const ctx = h.loadScripts([], win);
  t.after(() => ctx.__harness.dispose());
  const evaluator = ctx.__harness.evaluate("(" + worker.mainWorldEvaluator.toString() + ")");
  return {
    ctx,
    win,
    install: (channel, codes, ttlMs = 15000) => ({ ...evaluator(channel, codes, ttlMs) }),
  };
}

// Worker side of one wait: a fresh channel and the evaluator for the tree's custom_js.
function openWait(main, conditions, ttlMs) {
  const channel = require("crypto").randomUUID();
  const outcome = main.install(channel, worker.customJsCodes(conditions), ttlMs);
  assert.deepEqual(outcome, { ok: true });
  return channel;
}

function channelListeners(doc, channel) {
  let count = 0;
  for (const [type, list] of doc.__listeners) {
    if (type.startsWith(channel)) count += list.length;
  }
  return count;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function rejectsWith(promise, code) {
  const error = await promise.then(
    () => assert.fail(`expected rejection with ${code}`),
    (e) => e
  );
  assert.equal(error.code, code, `got ${error.code}: ${error.message}`);
  return error;
}

function visibleButton(doc, attrs = { id: "go" }) {
  const button = h.el(doc, "button", attrs, "Go");
  button.setRect({ x: 10, y: 10, width: 80, height: 20 });
  doc.body.append(button);
  return button;
}

// ── validation ───────────────────────────────────────────────────────────

test("unknown condition types reject with unknown_condition before waiting, also when nested", async (t) => {
  const { scope, win } = makeIsolated(t);
  assert.throws(() => scope.validateConditions([{ type: "glowing" }]), (e) => e.code === "unknown_condition");
  assert.throws(
    () => scope.validateConditions([{ type: "any_of", conditions: [[{ type: "visible" }], [{ type: "nope" }]] }]),
    (e) => e.code === "unknown_condition"
  );
  assert.throws(
    () => scope.validateConditions([{ type: "not", condition: [{ type: "all_of", conditions: [[{ type: "x" }]] }] }]),
    (e) => e.code === "unknown_condition"
  );
  assert.throws(() => scope.validateConditions([{ type: "count_eq", value: "2" }]), (e) => e.code === "invalid_argument");
  assert.throws(() => scope.validateConditions({ type: "visible" }), (e) => e.code === "invalid_argument");
  assert.equal(scope.validateConditions(undefined).length, 0);
  assert.throws(() => scope.evaluate(null, { type: "glowing" }, []), (e) => e.code === "unknown_condition");

  await rejectsWith(
    scope.waitForSelector({ selector: "#x", conditions: [{ type: "all_of", conditions: [[{ type: "sparkle" }]] }] }),
    "unknown_condition"
  );
  assert.equal(win.MutationObserver.instances.length, 0, "no observer: rejected before waiting");
  await rejectsWith(scope.waitForSelector({ selector: "#x", state: "shiny" }), "invalid_argument");
  await rejectsWith(scope.waitForSelector({ selector: "#x", timeoutMs: -1 }), "invalid_argument");
});

// ── null-safe evaluators ─────────────────────────────────────────────────

test("evaluators are null-safe: element conditions are false, detached/hidden true, counts use the match list", (t) => {
  const { scope, doc } = makeIsolated(t);
  for (const condition of [
    { type: "visible" },
    { type: "clickable" },
    { type: "unobscured" },
    { type: "text_not_empty" },
    { type: "text_contains", value: "a" },
    { type: "has_attribute", name: "id" },
    { type: "without_attribute", name: "id" },
    { type: "has_class", value: "c" },
    { type: "has_style", name: "color", value: "red" },
    { type: "stable" },
  ]) {
    assert.equal(scope.evaluate(null, condition, []), false, condition.type);
  }
  assert.equal(scope.evaluate(null, { type: "detached" }, []), true);
  assert.equal(scope.evaluate(null, { type: "hidden" }, []), true);
  assert.equal(scope.evaluate(null, { type: "count_eq", value: 0 }, []), true);
  assert.equal(scope.evaluate(null, { type: "count_gt", value: 0 }, []), false);
  assert.equal(scope.evaluate(null, { type: "not", condition: [{ type: "visible" }] }, []), true);

  const loose = h.el(doc, "div", { id: "loose" });
  assert.equal(scope.evaluate(loose, { type: "detached" }, [loose]), true, "disconnected counts as detached");
  const attached = visibleButton(doc);
  assert.equal(scope.evaluate(attached, { type: "detached" }, [attached]), false);
  assert.equal(scope.evaluate(attached, { type: "count_eq", value: 2 }, [attached, loose]), true);

  // No element: needs at least one condition, all holding for null, and a presence state.
  assert.equal(scope.checkElementConditions(null, undefined, [], []), false);
  assert.equal(scope.checkElementConditions(null, undefined, [{ type: "count_eq", value: 0 }], []), true);
  assert.equal(scope.checkElementConditions(null, "visible", [{ type: "detached" }], []), false);
  assert.equal(scope.checkElementConditions(null, "presence", [{ type: "detached" }, { type: "visible" }], []), false);
});

test("isVisible handles elements without offsetWidth (SVG) and prefers checkVisibility", (t) => {
  const { scope, doc } = makeIsolated(t);
  const svg = h.el(doc, "svg", { id: "icon" });
  svg.setRect({ x: 0, y: 0, width: 24, height: 24 });
  Object.defineProperty(svg, "offsetWidth", { value: undefined });
  Object.defineProperty(svg, "offsetHeight", { value: undefined });
  doc.body.append(svg);
  assert.equal(scope.isVisible(svg), true, "rendered SVG is visible");
  // display:none: Chrome returns no client rects; computed display is also checked.
  svg.__computedStyle = { display: "none" };
  svg.getClientRects = () => [];
  assert.equal(scope.isVisible(svg), false, "display:none SVG is hidden");
  assert.equal(scope.evaluate(svg, { type: "hidden" }, [svg]), true);

  const button = visibleButton(doc);
  assert.equal(scope.isVisible(button), true);
  const options = [];
  button.checkVisibility = (opts) => {
    options.push(opts);
    return false;
  };
  assert.equal(scope.isVisible(button), false, "checkVisibility wins when available");
  assert.deepEqual({ ...options[0] }, { checkOpacity: true, checkVisibilityCSS: true });
  button.checkVisibility = () => true;
  assert.equal(scope.isVisible(button), true);
  button.__computedStyle = { opacity: "0" };
  delete button.checkVisibility;
  assert.equal(scope.isVisible(button), false, "fallback checks opacity");
});

test("not(...) of an element condition holds for a missing element (element-less match)", async (t) => {
  const { scope } = makeIsolated(t);
  const result = await scope.waitForSelector({
    selector: "#absent", conditions: [{ type: "not", condition: [{ type: "has_class", value: "x" }] }], timeoutMs: 0,
  });
  assert.deepEqual({ ...result }, { element: null, matched: true, count: 0 });
});

test("count_eq(0) and hidden resolve {element:null, matched:true, count} when nothing matches", async (t) => {
  const { scope, serialized } = makeIsolated(t);
  assert.deepEqual(
    { ...(await scope.waitForSelector({ selector: ".row", conditions: [{ type: "count_eq", value: 0 }], timeoutMs: 0 })) },
    { element: null, matched: true, count: 0 }
  );
  assert.deepEqual(
    { ...(await scope.waitForSelector({ selector: "#gone", conditions: [{ type: "hidden" }], timeoutMs: 0 })) },
    { element: null, matched: true, count: 0 }
  );
  assert.equal(serialized.length, 0);
  // A non-presence state needs an element.
  await rejectsWith(
    scope.waitForSelector({ selector: "#gone", state: "visible", conditions: [{ type: "detached" }], timeoutMs: 0 }),
    "wait_timeout"
  );
});

test("detached resolves once the element is removed", async (t) => {
  const { scope, doc } = makeIsolated(t);
  const button = visibleButton(doc);
  const pending = scope.waitForSelector({ selector: "#go", conditions: [{ type: "detached" }], timeoutMs: 2000 });
  setTimeout(() => button.remove(), 60);
  const result = await pending;
  assert.equal(result.element, null);
  assert.equal(result.matched, true);
  assert.equal(result.count, 0);
});

test("count_eq resolves with the first element when the match list has the expected size", async (t) => {
  const { scope, doc } = makeIsolated(t);
  visibleButton(doc, { id: "a", class: "row" });
  visibleButton(doc, { id: "b", class: "row" });
  const result = await scope.waitForSelector({ selector: ".row", conditions: [{ type: "count_eq", value: 2 }], timeoutMs: 0 });
  assert.equal(result.id, "a");
});

// ── stable ───────────────────────────────────────────────────────────────

test("stable nested in any_of / all_of / not is detected; duration is the max", async (t) => {
  const { scope, doc } = makeIsolated(t);
  visibleButton(doc);
  const nested = [
    { type: "all_of", conditions: [[{ type: "any_of", conditions: [[{ type: "stable", duration: 40 }]] }]] },
    { type: "not", condition: [{ type: "all_of", conditions: [[{ type: "stable", duration: 150 }], [{ type: "detached" }]] }] },
  ];
  // timeout 0 cannot satisfy a stability window: proves the nested stable is honoured.
  await rejectsWith(scope.waitForSelector({ selector: "#go", conditions: [nested[0]], timeoutMs: 0 }), "wait_timeout");

  const started = Date.now();
  const result = await scope.waitForSelector({ selector: "#go", conditions: nested, timeoutMs: 3000 });
  const elapsed = Date.now() - started;
  assert.equal(result.id, "go");
  assert.ok(elapsed >= 140, `resolved after ${elapsed} ms, expected the 150 ms max duration`);
});

test("stable restarts its window when the element moves", async (t) => {
  const { scope, doc } = makeIsolated(t);
  const button = visibleButton(doc);
  const started = Date.now();
  const pending = scope.waitForSelector({ selector: "#go", state: "stable", conditions: [{ type: "stable", duration: 100 }], timeoutMs: 3000 });
  setTimeout(() => button.setRect({ x: 50, y: 10, width: 80, height: 20 }), 70);
  await pending;
  assert.ok(Date.now() - started >= 160, "the move at 70 ms restarted the 100 ms window");
});

test("the stable fingerprint uses bounded textContent, never innerText", async (t) => {
  const { scope, doc } = makeIsolated(t);
  const button = visibleButton(doc);
  let innerTextReads = 0;
  Object.defineProperty(button, "innerText", {
    get() {
      innerTextReads += 1;
      return "Go";
    },
  });
  const result = await scope.waitForSelector({ selector: "#go", state: "stable", conditions: [{ type: "stable", duration: 20 }], timeoutMs: 2000 });
  assert.equal(result.id, "go");
  assert.equal(innerTextReads, 0);
});

// ── tick errors and timeouts ─────────────────────────────────────────────

test("an exception in the seed rejects with internal instead of hanging", async (t) => {
  const { scope, doc } = makeIsolated(t);
  visibleButton(doc);
  scope.serializeElement = () => {
    throw new TypeError("boom");
  };
  const error = await rejectsWith(scope.waitForSelector({ selector: "#go", timeoutMs: 1000 }), "internal");
  assert.match(error.message, /boom/);
});

test("an exception in a later polling tick rejects with internal and stops the timers", async (t) => {
  const { scope, doc, ctx } = makeIsolated(t);
  let calls = 0;
  const original = doc.querySelectorAll.bind(doc);
  doc.querySelectorAll = (selector) => {
    calls += 1;
    if (calls >= 3) throw new TypeError("tick exploded");
    return original(selector);
  };
  const error = await rejectsWith(scope.waitForSelector({ selector: "#never", timeoutMs: 5000 }), "internal");
  assert.match(error.message, /tick exploded/);
  assert.equal(ctx.__harness.timers.intervals.size, 0, "interval cleared");
  assert.equal(ctx.__harness.timers.timeouts.size, 0, "timeout cleared");
});

test("an invalid selector rejects with invalid_argument", async (t) => {
  const { scope } = makeIsolated(t);
  await rejectsWith(scope.waitForSelector({ selector: "[[", timeoutMs: 100 }), "invalid_argument");
});

test("timeoutMs 0 is a single check that rejects wait_timeout with url and title", async (t) => {
  const { scope, doc } = makeIsolated(t, { url: "https://shop.test/cart", title: "Cart" });
  let calls = 0;
  const original = doc.querySelectorAll.bind(doc);
  doc.querySelectorAll = (selector) => {
    calls += 1;
    return original(selector);
  };
  const started = Date.now();
  const error = await rejectsWith(scope.waitForSelector({ selector: "#missing", timeoutMs: 0 }), "wait_timeout");
  assert.ok(Date.now() - started < 500, "0 is not turned into 10 s");
  assert.equal(calls, 1, "exactly one check");
  assert.match(error.message, /https:\/\/shop\.test\/cart/);
  assert.match(error.message, /Cart/);
});

test("a positive timeoutMs rejects wait_timeout after polling", async (t) => {
  const { scope } = makeIsolated(t);
  const started = Date.now();
  await rejectsWith(scope.waitForSelector({ selector: "#missing", timeoutMs: 120 }), "wait_timeout");
  assert.ok(Date.now() - started >= 100);
});

test("timeoutMs above 2^31-1 is clamped instead of overflowing setTimeout", async (t) => {
  const { scope, doc, ctx } = makeIsolated(t);
  const delays = [];
  const baseSetTimeout = ctx.setTimeout;
  ctx.setTimeout = (fn, ms, ...args) => {
    delays.push(ms);
    return baseSetTimeout(fn, ms, ...args);
  };
  let settled = false;
  const pending = scope.waitForSelector({ selector: "#late", timeoutMs: 3e9 });
  pending.then(() => (settled = true), () => (settled = true));
  assert.deepEqual(delays, [2147483647]);
  await sleep(60);
  assert.equal(settled, false, "an overflowing timer would have fired at once");
  visibleButton(doc, { id: "late" });
  assert.equal((await pending).id, "late");
});

// ── finders ──────────────────────────────────────────────────────────────

test("XPath that is not a node-set (TypeError) rejects with invalid_argument", async (t) => {
  const { scope, doc } = makeIsolated(t);
  doc.evaluate = () => {
    throw new TypeError("The result is not a node set, and therefore cannot be converted to the desired type.");
  };
  const error = await rejectsWith(scope.waitForSelector({ selector: "count(//a)", type: "xpath", timeoutMs: 100 }), "invalid_argument");
  assert.match(error.message, /not a node set/);
});

test("XPath waits only consider element nodes", async (t) => {
  const { scope, doc } = makeIsolated(t);
  const button = visibleButton(doc);
  const textNode = { nodeType: 3, textContent: "Go" };
  doc.evaluate = (expression, context, resolver, type) => {
    assert.equal(type, 7, "ORDERED_NODE_SNAPSHOT_TYPE");
    const items = [textNode, button];
    return { snapshotLength: items.length, snapshotItem: (i) => items[i] };
  };
  const result = await scope.waitForSelector({ selector: "//text()|//button", type: "xpath", timeoutMs: 0 });
  assert.equal(result.id, "go");
  const counted = await scope.waitForSelector({
    selector: "//x", type: "xpath", conditions: [{ type: "count_eq", value: 1 }], timeoutMs: 0,
  });
  assert.equal(counted.id, "go", "the text node is not counted");
});

test("text waits call scope.findElementsByText and scope.serializeElement at call time", async (t) => {
  const { scope, doc } = makeIsolated(t);
  const button = visibleButton(doc);
  scope.findElementsByText = (text, exact) => (text === "Go" && exact === false ? [button] : []);
  scope.serializeElement = (el) => ({ late: true, id: el.id });
  assert.deepEqual({ ...(await scope.waitForSelector({ selector: "Go", type: "text", timeoutMs: 0 })) }, { late: true, id: "go" });
});

// ── MutationObserver ─────────────────────────────────────────────────────

test("the observer ignores mutations caused by emunium itself", async (t) => {
  const { scope, doc, win, ctx } = makeIsolated(t);
  visibleButton(doc);
  ctx.__evaluations = 0;
  // custom_js that mutates (synchronously re-entering the fake observer) and queues records.
  const code =
    "globalThis.__evaluations += 1;" +
    "const o = MutationObserver.instances[0];" +
    "if (o) { o.records.push({ type: 'attributes', target: el }); triggerMutations(); }" +
    "return false;";
  const pending = scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code }], timeoutMs: 1500 });
  const observer = win.MutationObserver.instances[0];
  assert.ok(observer && observer.observing);
  assert.equal(ctx.__evaluations, 1, "seed");

  const ownNode = doc.createElement("div");
  scope.ownNodes.add(ownNode);
  for (let i = 0; i < 5; i += 1) observer.trigger([{ type: "childList", target: ownNode }]);
  await sleep(80);
  assert.equal(ctx.__evaluations, 1, "records on emunium nodes do not mark the page dirty");

  for (let i = 0; i < 5; i += 1) observer.trigger([{ type: "childList", target: doc.body }]);
  assert.equal(ctx.__evaluations, 1, "the observer never evaluates by itself");
  await sleep(80);
  assert.equal(ctx.__evaluations, 2, "five page batches -> one interval tick; our own re-entry did not re-dirty");
  assert.equal(observer.records.length, 0, "records queued by our own evaluation are dropped");

  await sleep(320);
  assert.ok(ctx.__evaluations >= 3, "idle re-check runs at least every 250 ms without mutations");
  observer.trigger([{ type: "childList", target: doc.body }]);
  await rejectsWith(pending, "wait_timeout");
});

test("without mutations the interval re-checks only every 250 ms, not every 50 ms", async (t) => {
  const { scope, doc, ctx } = makeIsolated(t);
  let calls = 0;
  const original = doc.querySelectorAll.bind(doc);
  doc.querySelectorAll = (selector) => {
    calls += 1;
    return original(selector);
  };
  await rejectsWith(scope.waitForSelector({ selector: "#missing", timeoutMs: 600 }), "wait_timeout");
  // seed + idle re-checks at ~250 and ~500 + final check at 600 (50 ms polling would be ~13).
  assert.ok(calls >= 3 && calls <= 5, `queried ${calls} times`);
  assert.equal(ctx.__harness.timers.intervals.size, 0);
});

// ── custom_js ────────────────────────────────────────────────────────────

test("custom_js isolated path: new Function(el, code); runtime errors are false; SyntaxError is script_error", async (t) => {
  const { scope, doc } = makeIsolated(t);
  const button = visibleButton(doc);
  assert.equal(scope.evaluate(button, { type: "custom_js", code: "return el.id === 'go';" }, [button]), true);
  assert.equal(scope.evaluate(null, { type: "custom_js", code: "return el === null;" }, []), true);
  assert.equal(scope.evaluate(button, { type: "custom_js", code: "throw new Error('not yet');" }, [button]), false);
  assert.throws(
    () => scope.evaluate(button, { type: "custom_js", code: "return (" }, [button]),
    (e) => e.code === "script_error"
  );
  const result = await scope.waitForSelector({
    selector: "#go", conditions: [{ type: "custom_js", code: "return el.textContent === 'Go';" }], timeoutMs: 0,
  });
  assert.equal(result.id, "go");
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return )" }], timeoutMs: 500 }),
    "script_error"
  );
});

test("custom_js falls back to the MAIN world through the wait's channel when isolated code generation is blocked", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  const button = visibleButton(doc);
  const main = mainWorld(t, doc);
  const windowKeys = Object.keys(main.win).sort();
  const conditions = [{ type: "custom_js", code: "return el.id === 'go' && typeof window === 'object';" }];
  const channel = openWait(main, conditions);
  assert.equal(channelListeners(doc, channel), 4, "eval, async, ready and close listeners on document");

  const result = await scope.waitForSelector({ selector: "#go", conditions, timeoutMs: 0, __mainChannel: channel });
  assert.equal(result.id, "go");
  assert.equal(scope.customJsState.isolatedBlocked, true);
  assert.equal(channelListeners(doc, channel), 0, "the wait end closed the channel");
  assert.deepEqual(button.getAttributeNames(), ["id"], "no attributes on page elements");
  assert.deepEqual(Object.keys(main.win).sort(), windowKeys, "nothing added to the MAIN window");
});

test("MAIN-world custom_js results: truthy true, falsy false, exception false, null element, nested trees", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  const button = visibleButton(doc);
  const main = mainWorld(t, doc);
  const leaf = (code) => ({ type: "custom_js", code });
  const check = (conditions, el) => {
    const channel = openWait(main, conditions);
    assert.ok(scope.openMainChannel(conditions, channel));
    try {
      return scope.checkElementConditions(el, "presence", conditions, el ? [el] : []);
    } finally {
      scope.closeMainChannel(conditions);
      assert.equal(channelListeners(doc, channel), 0);
    }
  };
  assert.equal(check([leaf("return el.id;")], button), true, "truthy");
  assert.equal(check([leaf("return 0;")], button), false, "falsy");
  assert.equal(check([leaf("throw new Error('not yet');")], button), false, "exception");
  assert.equal(check([leaf("return el === null;")], null), true, "no element: dispatched on document, el is null");
  assert.equal(check([leaf("return el === null;")], button), false);
  // Index mapping across groups: a wrong index would evaluate another leaf's code.
  const nested = [
    leaf("return el.id === 'go';"),
    { type: "all_of", conditions: [[{ type: "not", condition: [leaf("return false;")] }], [leaf("return el.tagName === 'BUTTON';")]] },
    { type: "not", condition: [{ type: "any_of", conditions: [[leaf("return el.id === 'nope';")]] }] },
  ];
  assert.equal(check(nested, button), true);
  assert.throws(
    () => scope.checkElementConditions(button, "presence", [leaf("return true;")], [button]),
    (e) => e.code === "content_unavailable",
    "an array without a channel cannot reach the MAIN world"
  );
});

test("custom_js returning a Promise/thenable rejects script_error (isolated and MAIN world)", async (t) => {
  const isolated = makeIsolated(t);
  const button = visibleButton(isolated.doc);
  for (const code of ["return Promise.resolve(true);", "return { then() {} };"]) {
    assert.throws(
      () => isolated.scope.evaluate(button, { type: "custom_js", code }, [button]),
      (e) => e.code === "script_error" && /synchronously/.test(e.message)
    );
  }
  await rejectsWith(
    isolated.scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return (async () => true)();" }], timeoutMs: 200 }),
    "script_error"
  );

  const main = makeIsolated(t, { blockEval: true });
  visibleButton(main.doc);
  const world = mainWorld(t, main.doc);
  for (const code of ["return Promise.resolve(true);", "return { then() {} };"]) {
    const conditions = [{ type: "custom_js", code }];
    const channel = openWait(world, conditions);
    const error = await rejectsWith(
      main.scope.waitForSelector({ selector: "#go", conditions, timeoutMs: 200, __mainChannel: channel }),
      "script_error"
    );
    assert.match(error.message, /synchronously/);
    assert.equal(channelListeners(main.doc, channel), 0, "a rejected wait closes its channel too");
  }
});

test("MAIN evaluator compiles every code up front: csp_blocked / script_error, and nothing is registered", (t) => {
  const { doc } = makeIsolated(t, { blockEval: true });
  const csp = mainWorld(t, doc, { csp: true });
  const channel = require("crypto").randomUUID();
  const blocked = csp.install(channel, ["return true;"]);
  assert.equal(blocked.ok, false);
  assert.equal(blocked.code, "csp_blocked");
  assert.match(blocked.message, /unsafe-eval/);
  const plainWorld = mainWorld(t, doc);
  const broken = plainWorld.install(channel, ["return true;", "return ("]);
  assert.equal(broken.ok, false);
  assert.equal(broken.code, "script_error");
  assert.equal(channelListeners(doc, channel), 0);
  assert.equal(doc.defaultView.__listeners.size, 0);
});

test("MAIN listeners are removed on the close event and after ttlMs", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  const button = visibleButton(doc);
  const main = mainWorld(t, doc);
  const conditions = [{ type: "custom_js", code: "return el.hasAttribute('data-ready');" }];
  const channel = openWait(main, conditions);
  const waiting = scope.waitForSelector({ selector: "#go", conditions, timeoutMs: 2000, __mainChannel: channel });
  await sleep(60);
  assert.equal(channelListeners(doc, channel), 4, "open while the wait runs");
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return 1;" }], timeoutMs: 0, __mainChannel: "not-a-uuid" }),
    "content_unavailable"
  );
  assert.equal(channelListeners(doc, channel), 4, "another wait does not close this channel");
  button.setAttribute("data-ready", "");
  assert.equal((await waiting).id, "go");
  assert.equal(channelListeners(doc, channel), 0, "close event at wait end");
  assert.equal(main.ctx.__harness.timers.timeouts.size, 0, "and its ttl timer is cleared");

  const timedOut = openWait(main, [{ type: "custom_js", code: "return false;" }]);
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return false;" }], timeoutMs: 80, __mainChannel: timedOut }),
    "wait_timeout"
  );
  assert.equal(channelListeners(doc, timedOut), 0, "a timed-out wait closes its channel");

  const expiring = openWait(main, conditions, 40);
  assert.equal(channelListeners(doc, expiring), 4);
  await sleep(90);
  assert.equal(channelListeners(doc, expiring), 0, "ttl removed them without a close event");
  assert.equal(main.ctx.__harness.timers.timeouts.size, 0);
});

test("a page that does not know the channel cannot observe, answer or close it", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  const button = visibleButton(doc);
  const main = mainWorld(t, doc);
  const seen = [];
  const guesses = ["__emunium_main_hello", "__emunium_main_ready", "__emunium_main_eval", "0", ":0", "0:async", "close", ":close", "custom_js"];
  const forged = require("crypto").randomUUID();
  for (const type of guesses.concat([`${forged}:0`, `${forged}:close`])) {
    for (const target of [doc, doc.defaultView, main.win, button]) {
      for (const capture of [true, false]) {
        target.addEventListener(type, (event) => {
          seen.push(type);
          event.preventDefault();
        }, capture);
      }
    }
  }
  const conditions = [{ type: "custom_js", code: "return el.hasAttribute('data-ready');" }];
  const channel = openWait(main, conditions);
  // The page answers / closes on a channel it made up: no effect on the real one.
  doc.dispatchEvent(new main.win.Event(`${forged}:close`));
  assert.equal(channelListeners(doc, channel), 4);
  seen.length = 0;
  // A page listener on the element for the real type is never reached: the MAIN capture
  // listener on document stops propagation.
  let reachedElement = 0;
  button.addEventListener(`${channel}:0`, () => (reachedElement += 1));
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions, timeoutMs: 120, __mainChannel: channel }),
    "wait_timeout"
  );
  assert.deepEqual(seen, [], "no guessed listener fired");
  assert.equal(reachedElement, 0);
  assert.deepEqual(button.getAttributeNames(), ["id"]);
});

test("two concurrent waits use different channels and do not interfere", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  visibleButton(doc, { id: "a" });
  visibleButton(doc, { id: "b" });
  const main = mainWorld(t, doc);
  main.ctx.counts = { a: 0, b: 0 };
  const waitA = [{ type: "custom_js", code: "counts.a += 1; return globalThis.readyA === true;" }];
  const waitB = [{ type: "custom_js", code: "counts.b += 1; return globalThis.readyB === true;" }];
  const channelA = openWait(main, waitA);
  const channelB = openWait(main, waitB);
  assert.notEqual(channelA, channelB);
  const pendingA = scope.waitForSelector({ selector: "#a", conditions: waitA, timeoutMs: 3000, __mainChannel: channelA });
  const pendingB = scope.waitForSelector({ selector: "#b", conditions: waitB, timeoutMs: 3000, __mainChannel: channelB });
  main.ctx.readyB = true;
  assert.equal((await pendingB).id, "b");
  assert.equal(channelListeners(doc, channelB), 0, "B closed");
  assert.equal(channelListeners(doc, channelA), 4, "A still open");
  const countA = main.ctx.counts.a;
  assert.ok(countA >= 1);
  main.ctx.readyA = true;
  assert.equal((await pendingA).id, "a");
  assert.equal(channelListeners(doc, channelA), 0);
  assert.ok(main.ctx.counts.a > countA, "A kept evaluating its own code");

  // The same conditions array in two waits: each wait binds its own copy.
  const shared = [{ type: "custom_js", code: "return true;" }];
  const [one, two] = [openWait(main, shared), openWait(main, shared)];
  const results = await Promise.all([
    scope.waitForSelector({ selector: "#a", conditions: shared, timeoutMs: 0, __mainChannel: one }),
    scope.waitForSelector({ selector: "#b", conditions: shared, timeoutMs: 0, __mainChannel: two }),
  ]);
  assert.deepEqual(results.map((r) => r.id), ["a", "b"]);
  assert.equal(channelListeners(doc, one) + channelListeners(doc, two), 0);
});

test("worker customJsCodes and content customJsLeaves walk a nested tree in the same order", (t) => {
  const { scope } = makeIsolated(t);
  const leaf = (code) => ({ type: "custom_js", code });
  const tree = [
    leaf("c0"),
    { type: "visible" },
    {
      type: "any_of",
      conditions: [
        [leaf("c1"), { type: "not", condition: [leaf("c2"), { type: "all_of", conditions: [[leaf("c3")], [leaf("c4")]] }] }],
        [{ type: "has_class", value: "x" }, leaf("c5")],
      ],
    },
    { type: "not", condition: [{ type: "any_of", conditions: [[leaf("c6")]] }] },
    leaf("c7"),
  ];
  scope.validateConditions(tree);
  const expected = ["c0", "c1", "c2", "c3", "c4", "c5", "c6", "c7"];
  assert.deepEqual([...scope.customJsLeaves(tree)].map((c) => c.code), expected);
  assert.deepEqual([...worker.customJsCodes(tree)], expected);
  assert.deepEqual([...worker.customJsCodes([{ type: "visible" }])], []);
  assert.equal(worker.customJsCodes([leaf(42)]), null, "invalid code: the content script rejects it, nothing injected");
});

test("a running custom_js wait keeps its channel across content-script re-injection", async (t) => {
  const { ctx, scope, doc } = makeIsolated(t, { blockEval: true });
  const button = visibleButton(doc);
  const main = mainWorld(t, doc);
  const conditions = [{ type: "custom_js", code: "return el.hasAttribute('data-ready');" }];
  const channel = openWait(main, conditions);
  const pending = scope.waitForSelector({ selector: "#go", conditions, timeoutMs: 2000, __mainChannel: channel });
  h.loadScripts(["content_conditions.js", "content_wait.js"], ctx);
  assert.equal(ctx.EmuniumContent.mainChannels, scope.mainChannels, "the channel map survived");
  await sleep(60);
  button.setAttribute("data-ready", "");
  assert.equal((await pending).id, "go", "the old wait still evaluates through the new scripts");
  assert.equal(channelListeners(doc, channel), 0);
});

test("a missing evaluator never reads as false: not(custom_js) rejects content_unavailable", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  visibleButton(doc);
  const main = mainWorld(t, doc);
  const notCustom = () => [{ type: "not", condition: [{ type: "custom_js", code: "return false;" }] }];
  // Other document: a valid channel the page never received an evaluator for.
  const otherDocument = require("crypto").randomUUID();
  const error = await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: notCustom(), timeoutMs: 300, __mainChannel: otherDocument }),
    "content_unavailable"
  );
  assert.match(error.message, /evaluator for this wait is gone/);
  // ttl expired before the wait ran.
  const expired = openWait(main, notCustom(), 10);
  await sleep(40);
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: notCustom(), timeoutMs: 300, __mainChannel: expired }),
    "content_unavailable"
  );
  // Expiry in the middle of a wait rejects at the next tick instead of resolving.
  const custom = [{ type: "custom_js", code: "return false;" }];
  const midway = openWait(main, custom, 120);
  const started = Date.now();
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: custom, timeoutMs: 1000, __mainChannel: midway }),
    "content_unavailable"
  );
  assert.ok(Date.now() - started < 900, "rejected at the first tick after expiry, not at the timeout");
  // Live evaluator: not(false) holds.
  const live = openWait(main, notCustom());
  assert.equal((await scope.waitForSelector({ selector: "#go", conditions: notCustom(), timeoutMs: 0, __mainChannel: live })).id, "go");
});

test("ttl boundary: the evaluator outlives the wait's final tick only with the margin", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  visibleButton(doc);
  const main = mainWorld(t, doc);
  const timeoutMs = 150;
  // No mutations: only the seed (false) and the final tick at timeoutMs (true) evaluate.
  const run = (ttlMs) => {
    main.ctx.startedAt = Date.now();
    const conditions = [{ type: "custom_js", code: "return Date.now() - startedAt >= 140;" }];
    const channel = openWait(main, conditions, ttlMs);
    return scope.waitForSelector({ selector: "#go", conditions, timeoutMs, __mainChannel: channel });
  };
  await rejectsWith(run(timeoutMs - 50), "content_unavailable"); // expired just before the final tick
  // The worker's ttl, scaled down: timeoutMs + grace + margin keeps it alive.
  const scaled = worker.mainEvaluatorTtl(timeoutMs) - 7000 + 80;
  assert.equal(worker.mainEvaluatorTtl(timeoutMs) - timeoutMs, 7000);
  assert.equal((await run(scaled)).id, "go", "the final tick still reaches the evaluator");
});

test("custom_js without the MAIN evaluator rejects content_unavailable", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  visibleButton(doc);
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return true;" }], timeoutMs: 500 }),
    "content_unavailable"
  );
});

test("the wait hook survives re-injection and wraps the published waitForSelector once", (t) => {
  const { ctx, scope } = makeIsolated(t);
  const calls = [];
  scope.waitForSelector = (params) => {
    calls.push(params);
    return Promise.resolve("raw");
  };
  h.loadScripts(["content_conditions.js"], ctx);
  const channel = require("crypto").randomUUID();
  const conditions = [{ type: "custom_js", code: "return 1" }];
  return scope.waitForSelector({ selector: "#go", conditions, __mainChannel: channel }).then((value) => {
    assert.equal(value, "raw");
    assert.equal(calls.length, 1, "not wrapped twice");
    assert.notEqual(calls[0].conditions, conditions, "each wait binds its own copy");
    assert.deepEqual(calls[0].conditions, conditions);
  });
});

// ── manifest ─────────────────────────────────────────────────────────────

test("manifest: 2.0.0, Chrome 116+, userScripts and alarms, no web_accessible_resources, no MAIN-world file", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(h.EXTENSION_DIR, "manifest.json"), "utf8"));
  assert.equal(manifest.version, "2.0.0");
  assert.equal(manifest.minimum_chrome_version, "116");
  for (const permission of ["userScripts", "alarms", "scripting"]) {
    assert.ok(manifest.permissions.includes(permission), permission);
  }
  assert.equal("web_accessible_resources" in manifest, false);
  const declared = manifest.content_scripts.flatMap((entry) => entry.js);
  assert.deepEqual(declared, [
    "content_shared.js",
    "content_queries.js",
    "content_conditions.js",
    "content_wait.js",
    "content.js",
  ]);
  assert.equal(fs.existsSync(path.join(h.EXTENSION_DIR, "content_main.js")), false, "the evaluator is a worker func");
});
