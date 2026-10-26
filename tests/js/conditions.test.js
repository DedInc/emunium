"use strict";
// tests/js/conditions.test.js - content_conditions.js, content_wait.js and content_main.js:
// validation, null-safe evaluators, element-less matches, nested stable, tick errors,
// timeouts, custom_js (isolated and MAIN world) and the manifest
// (docs/architecture/bridge-protocol.md sections 6-7).

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

// MAIN world: content_main.js in its own context over the same document.
function loadMain(t, doc, { csp = false } = {}) {
  const isolatedView = doc.defaultView;
  const win = h.createWindow(doc);
  doc.defaultView = isolatedView;
  if (csp) {
    win.Function = function () {
      throw new EvalError("Refused to evaluate a string as JavaScript because 'unsafe-eval' is not allowed");
    };
  }
  const ctx = h.loadScripts(["content_main.js"], win);
  t.after(() => ctx.__harness.dispose());
  return ctx;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function listenerCount(target, type) {
  return target.__listeners && target.__listeners.get(type) ? target.__listeners.get(type).length : 0;
}

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

test("custom_js falls back to the MAIN world when isolated code generation is blocked", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  const button = visibleButton(doc);
  loadMain(t, doc);
  assert.equal(listenerCount(doc, "__emunium_main_hello"), 1, "armed for the handshake");

  const result = await scope.waitForSelector({
    selector: "#go", conditions: [{ type: "custom_js", code: "return el.id === 'go' && typeof window === 'object';" }], timeoutMs: 0,
  });
  assert.equal(result.id, "go");
  assert.equal(listenerCount(doc, "__emunium_main_hello"), 0, "no document listener after the handshake");
  assert.equal(listenerCount(doc.defaultView, "__emunium_main_hello"), 0);
  assert.deepEqual(button.getAttributeNames(), ["id"], "no attributes on page elements");
  assert.equal(scope.mainChannel.isolatedBlocked, true);

  assert.equal(scope.evaluate(null, { type: "custom_js", code: "return el === null;" }, []), true);
  assert.equal(scope.evaluate(button, { type: "custom_js", code: "throw new Error('x');" }, [button]), false);
  assert.throws(
    () => scope.evaluate(button, { type: "custom_js", code: "return (" }, [button]),
    (e) => e.code === "script_error"
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
  loadMain(t, main.doc);
  const error = await rejectsWith(
    main.scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return Promise.resolve(true);" }], timeoutMs: 200 }),
    "script_error"
  );
  assert.match(error.message, /synchronously/);
});

test("content_main.js installs once: a second run re-arms the same port", (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  const button = visibleButton(doc);
  loadMain(t, doc);
  loadMain(t, doc);
  assert.equal(listenerCount(doc, "__emunium_main_hello"), 1);
  assert.equal(scope.evaluate(button, { type: "custom_js", code: "return true;" }, [button]), true);
  assert.equal(listenerCount(doc, "__emunium_main_hello"), 0);
});

test("custom_js without the MAIN evaluator rejects content_unavailable", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  visibleButton(doc);
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return true;" }], timeoutMs: 500 }),
    "content_unavailable"
  );
});

test("custom_js under a page CSP that forbids eval rejects csp_blocked", async (t) => {
  const { scope, doc } = makeIsolated(t, { blockEval: true });
  visibleButton(doc);
  loadMain(t, doc, { csp: true });
  await rejectsWith(
    scope.waitForSelector({ selector: "#go", conditions: [{ type: "custom_js", code: "return true;" }], timeoutMs: 500 }),
    "csp_blocked"
  );
});

// ── manifest ─────────────────────────────────────────────────────────────

test("manifest: 2.0.0, Chrome 111+, userScripts and alarms, no web_accessible_resources, content_main.js undeclared", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(h.EXTENSION_DIR, "manifest.json"), "utf8"));
  assert.equal(manifest.version, "2.0.0");
  assert.equal(manifest.minimum_chrome_version, "111");
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
  assert.ok(!declared.includes("content_main.js"));
  assert.ok(fs.existsSync(path.join(h.EXTENSION_DIR, "content_main.js")));
});
