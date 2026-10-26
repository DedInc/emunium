"use strict";
// tests/js/content.test.js - content_shared.js, content_queries.js and content.js:
// per-document element ids and staleness, deepest text match, geometry payload, the v2
// message envelope and error codes, idempotent init and pointerProbe
// (docs/architecture/bridge-protocol.md sections 6-7, native-input.md section 4).

const test = require("node:test");
const assert = require("node:assert/strict");
const h = require("./harness");

const plain = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const FILES = ["content_shared.js", "content_queries.js", "content.js"];
const ALL_FILES = ["content_shared.js", "content_queries.js", "content_conditions.js", "content_wait.js", "content.js"];

// Content world with scope.waitForSelector stubbed (S4 owns the real one).
function makeContent(t, { doc, overrides = {}, files = FILES, waitForSelector } = {}) {
  const document = doc || h.createDocument({ url: "https://example.test/page", title: "Page" });
  const chrome = h.createFakeChrome();
  const win = h.createWindow(document, Object.assign({ chrome }, overrides));
  win.top = win;
  const waitCalls = [];
  if (files === FILES) {
    win.EmuniumContent = {
      waitForSelector:
        waitForSelector ||
        (async (params) => {
          waitCalls.push(params);
          return { element: null, matched: true, count: 0 };
        }),
    };
  }
  const ctx = h.loadScripts(files, win);
  t.after(() => ctx.__harness.dispose());
  // Chrome messaging is JSON: the round-trip also moves values out of the vm realm.
  const send = (method, params = {}, extra = {}) =>
    h
      .dispatchMessage(chrome.runtime.onMessage, Object.assign({ __emunium__: 2, id: 1, method, params }, extra))
      .then(plain);
  return { ctx, win, doc: document, chrome, scope: ctx.EmuniumContent, send, waitCalls };
}

function button(doc, attrs = { id: "go" }, text = "Go", rect = { x: 10, y: 20, width: 80, height: 30 }) {
  const element = h.el(doc, "button", attrs, text);
  element.setRect(rect);
  doc.body.append(element);
  return element;
}

async function okResult(promise) {
  const response = await promise;
  assert.equal(response.ok, true, JSON.stringify(response));
  return response.result;
}

async function errorCode(promise) {
  const response = await promise;
  assert.equal(response.ok, false, JSON.stringify(response));
  assert.equal(typeof response.error.message, "string");
  return response.error.code;
}

function move(target, init) {
  target.dispatchEvent(new h.FakePointerEvent("pointermove", Object.assign({ bubbles: true, isTrusted: true }, init)));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ── element ids ──────────────────────────────────────────────────────────

test("ids are <documentToken>.<n>, stable per element, and differ between documents", async (t) => {
  const a = makeContent(t);
  const go = button(a.doc);
  const other = button(a.doc, { id: "other" }, "Other");
  const first = await okResult(a.send("querySelector", { selector: "#go" }));
  const again = await okResult(a.send("querySelector", { selector: "#go" }));
  const second = await okResult(a.send("querySelector", { selector: "#other" }));
  const match = /^([0-9a-f]{12})\.(\d+)$/.exec(first.elementId);
  assert.ok(match, first.elementId);
  assert.equal(again.elementId, first.elementId);
  assert.equal(second.elementId, `${match[1]}.${Number(match[2]) + 1}`);
  assert.equal(a.scope.resolveElement(first.elementId), go);
  assert.equal(a.scope.resolveElement(second.elementId), other);

  const b = makeContent(t);
  button(b.doc);
  const foreign = await okResult(b.send("querySelector", { selector: "#go" }));
  assert.notEqual(foreign.elementId.split(".")[0], match[1], "a new document gets a new token");
  assert.equal(await errorCode(b.send("describeElement", { elementId: first.elementId })), "stale_element");
  assert.equal(await errorCode(a.send("describeElement", { elementId: "e_1" })), "stale_element");
  assert.equal(await errorCode(a.send("describeElement", { elementId: `${match[1]}.999` })), "stale_element");
  assert.equal(await errorCode(a.send("describeElement", { elementId: 7 })), "invalid_argument");
});

test("disconnected and collected nodes are stale_element; ids are never reused", async (t) => {
  const dead = new Set();
  class TestWeakRef {
    constructor(target) {
      this.target = target;
    }
    deref() {
      return dead.has(this.target) ? undefined : this.target;
    }
  }
  const { doc, send, scope } = makeContent(t, { overrides: { WeakRef: TestWeakRef, FinalizationRegistry: undefined } });
  const go = button(doc);
  const gone = button(doc, { id: "gone" }, "Gone");
  const goId = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const goneId = (await okResult(send("querySelector", { selector: "#gone" }))).elementId;

  go.remove();
  assert.equal(await errorCode(send("describeElement", { elementId: goId })), "stale_element");
  assert.equal(await errorCode(send("focus", { elementId: goId })), "stale_element");
  doc.body.append(go);
  assert.equal((await okResult(send("describeElement", { elementId: goId }))).elementId, goId, "reattached keeps its id");

  dead.add(gone);
  assert.equal(await errorCode(send("getAttribute", { elementId: goneId, name: "id" })), "stale_element");
  assert.equal(scope.state.refs.has(goneId), false, "collected entry is dropped");
  const fresh = button(doc, { id: "fresh" }, "Fresh");
  const freshId = scope.getElementId(fresh);
  assert.notEqual(freshId, goneId);
  assert.notEqual(freshId, goId);
  assert.ok(Number(freshId.split(".")[1]) > Number(goneId.split(".")[1]));
});

// ── text matching ────────────────────────────────────────────────────────

test("findElementsByText returns only the deepest matches and skips script/style/noscript", (t) => {
  const { doc, scope } = makeContent(t);
  const span = h.el(doc, "span", {}, "Sign in");
  const link = h.el(doc, "a", { href: "/login" }, [span]);
  const wrap = h.el(doc, "div", { id: "wrap" }, [
    link,
    h.el(doc, "script", {}, "sign in"),
    h.el(doc, "style", {}, ".sign-in{}"),
    h.el(doc, "noscript", {}, "Sign in"),
  ]);
  const save1 = h.el(doc, "button", {}, "Save");
  const save2 = h.el(doc, "button", {}, "Save");
  doc.body.append(wrap, h.el(doc, "div", {}, [save1, save2]));

  assert.deepEqual(Array.from(scope.findElementsByText("sign in", false)), [span]);
  assert.deepEqual(Array.from(scope.findElementsByText("Sign in", true)), [span]);
  assert.deepEqual(Array.from(scope.findElementsByText("Save", true)), [save1, save2]);
  assert.deepEqual(Array.from(scope.findElementsByText("nowhere", false)), []);

  // exact: the parent's text differs from the child's, so the parent matches alone
  const label = h.el(doc, "label", {}, [h.el(doc, "b", {}, "Name"), h.el(doc, "i", {}, "*")]);
  doc.body.append(label);
  assert.deepEqual(Array.from(scope.findElementsByText("Name*", true)), [label]);
});

test("queryByText serializes deepest matches; non-string text is invalid_argument", async (t) => {
  const { doc, send } = makeContent(t);
  doc.body.append(h.el(doc, "div", {}, [h.el(doc, "p", { id: "hello" }, "Hello world")]));
  const result = await okResult(send("queryByText", { text: "hello", exact: false }));
  assert.deepEqual(result.map((item) => item.attrs.id), ["hello"]);
  assert.equal(await errorCode(send("queryByText", { text: 5 })), "invalid_argument");
});

// ── geometry ─────────────────────────────────────────────────────────────

test("element payload carries rect, geometry and the legacy DIP estimate", async (t) => {
  const overrides = {
    screenX: 100,
    screenY: 50,
    outerWidth: 1296,
    outerHeight: 1000,
    innerWidth: 1280,
    innerHeight: 900,
    devicePixelRatio: 1.875,
  };
  const { doc, win, send } = makeContent(t, { overrides });
  win.visualViewport = Object.assign({}, win.visualViewport, { offsetLeft: 3, offsetTop: 4, scale: 2 });
  button(doc, { id: "go", class: "primary" }, "Go", { x: 10, y: 20, width: 80, height: 30 });

  const payload = await okResult(send("querySelector", { selector: "#go" }));
  assert.equal(payload.tag, "button");
  assert.deepEqual(payload.attrs, { id: "go", class: "primary" });
  assert.equal(payload.text, "Go");
  assert.equal(payload.visible, true);
  assert.deepEqual(payload.rect, { x: 10, y: 20, width: 80, height: 30 });
  const geometry = {
    screenX: 100,
    screenY: 50,
    outerWidth: 1296,
    outerHeight: 1000,
    innerWidth: 1280,
    innerHeight: 900,
    devicePixelRatio: 1.875,
    visualViewport: { offsetLeft: 3, offsetTop: 4, scale: 2 },
    scrollWidth: 1280,
    scrollHeight: 900,
    clientWidth: 1280,
    clientHeight: 900,
  };
  assert.deepEqual(payload.geometry, geometry);
  // border 8, origin (108, 50 + 1000 - 900 - 8), centre (50, 35)
  assert.equal(payload.absoluteScreenX, 158);
  assert.equal(payload.absoluteScreenY, 177);

  const info = await okResult(send("pageInfo"));
  assert.deepEqual(info.geometry, geometry);
  assert.equal(info.url, "https://example.test/page");
  assert.equal(info.title, "Page");
  for (const key of ["readyState", "scrollX", "scrollY", "scrollWidth", "scrollHeight"]) {
    assert.ok(key in info, key);
  }
});

test("geometry carries the scrolling element extent and the viewport without scrollbars", async (t) => {
  const { doc, send } = makeContent(t);
  button(doc);
  // a 4000 px tall page: a 15 px vertical scrollbar narrows the root's client box
  doc.documentElement.scrollWidth = 1265;
  doc.documentElement.scrollHeight = 4000;
  doc.documentElement.clientWidth = 1265;
  doc.documentElement.clientHeight = 900;
  for (const geometry of [
    (await okResult(send("querySelector", { selector: "#go" }))).geometry,
    (await okResult(send("pageInfo"))).geometry,
  ]) {
    assert.equal(geometry.scrollWidth, 1265);
    assert.equal(geometry.scrollHeight, 4000);
    assert.equal(geometry.clientWidth, 1265);
    assert.equal(geometry.clientHeight, 900);
    assert.equal(geometry.innerWidth, 1280);
  }
});

test("scrollIntoView scrolls instantly to the centre and returns the element payload", async (t) => {
  const { doc, send, win } = makeContent(t);
  const go = button(doc);
  go.__onScrollIntoView = () => go.setRect({ x: 10, y: 300, width: 80, height: 30 });
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const payload = await okResult(send("scrollIntoView", { elementId: id }));
  assert.deepEqual(plain(go.scrollIntoViewCalls), [{ behavior: "instant", block: "center" }]);
  assert.equal(payload.elementId, id);
  assert.equal(payload.rect.y, 300);
  assert.ok(payload.geometry);

  assert.deepEqual(await okResult(send("scrollTo", { x: 0, y: 120 })), { scrollX: 0, scrollY: 120 });
  assert.equal(win.scrollCalls.at(-1).behavior, "instant");
  assert.equal(await errorCode(send("scrollTo", { x: "0", y: 1 })), "invalid_argument");
});

// ── envelope and errors ──────────────────────────────────────────────────

test("only __emunium__ === 2 messages are answered, as {ok, result}", async (t) => {
  const { send, chrome } = makeContent(t);
  for (const marker of [true, 1, "2", undefined]) {
    const reply = await h.dispatchMessage(chrome.runtime.onMessage, { __emunium__: marker, id: 1, method: "ping" });
    assert.equal(reply, undefined, `marker ${String(marker)}`);
  }
  assert.deepEqual(await send("ping"), { ok: true, result: { pong: true, url: "https://example.test/page" } });
  assert.deepEqual(await send("querySelector", { selector: "#missing" }), { ok: true, result: null });
  assert.deepEqual(await send("querySelectorAll", { selector: "p" }), { ok: true, result: [] });
});

test("error codes: invalid selector/XPath, unknown method, coded and plain exceptions", async (t) => {
  let failure = null;
  const { doc, send, waitCalls } = makeContent(t, {
    waitForSelector: async (params) => {
      waitCalls.push(params);
      if (failure) throw failure;
      return { element: null, matched: true, count: 0 };
    },
  });
  assert.equal(await errorCode(send("querySelector", { selector: "div[" })), "invalid_argument");
  assert.equal(await errorCode(send("querySelectorAll", { selector: "" })), "invalid_argument");
  doc.evaluate = () => {
    const error = new Error("The string '//[' is not a valid XPath expression.");
    error.name = "SyntaxError";
    throw error;
  };
  assert.equal(await errorCode(send("queryXPath", { xpath: "//[" })), "invalid_argument");
  assert.equal(await errorCode(send("executeScript", { code: "1" })), "unknown_method");
  assert.equal(await errorCode(send("getElementCoords", { elementId: "x" })), "unknown_method");
  assert.equal(await errorCode(send("toString")), "unknown_method");

  const params = { selector: "#x", type: "css", timeoutMs: 250, state: "visible" };
  assert.deepEqual(await okResult(send("waitForSelector", params)), { element: null, matched: true, count: 0 });
  assert.deepEqual(waitCalls.at(-1), params);
  failure = Object.assign(new Error("waited 250 ms"), { code: "wait_timeout" });
  assert.deepEqual(await send("waitForSelector", params), {
    ok: false,
    error: { code: "wait_timeout", message: "waited 250 ms" },
  });
  failure = new Error("boom");
  assert.deepEqual(await send("waitForSelector", params), { ok: false, error: { code: "internal", message: "boom" } });
});

test("re-injection keeps one listener, one ready message, one pointer listener and the token", async (t) => {
  const { ctx, doc, chrome, send, scope, win } = makeContent(t);
  button(doc);
  const before = await okResult(send("querySelector", { selector: "#go" }));
  h.loadScripts(FILES, ctx);
  h.loadScripts(FILES, ctx);
  assert.equal(chrome.runtime.onMessage.listeners.length, 1);
  const ready = chrome.runtime.sendMessage.calls.filter(([msg]) => msg && msg.__emunium_ready__ !== undefined);
  assert.equal(ready.length, 1);
  assert.deepEqual(plain(ready[0][0]), { __emunium_ready__: 2, url: "https://example.test/page" });
  assert.equal(chrome.runtime.sendMessage.calls.length, 1, "no executeScript round-trip through the worker");
  assert.equal(win.__listeners.get("pointermove").length, 1);
  assert.equal(win.__listeners.get("mousemove").length, 1);
  assert.equal(win.__listeners.get("dragover").length, 1);
  assert.equal(win.__listeners.get("dragleave").length, 1);
  assert.equal((await okResult(send("describeElement", { elementId: before.elementId }))).elementId, before.elementId);
  assert.equal(scope.state.token, before.elementId.split(".")[0]);
});

test("subframes do not announce readiness; the full manifest order loads and delegates waits", async (t) => {
  const doc = h.createDocument({ url: "https://example.test/frame" });
  const chrome = h.createFakeChrome();
  const win = h.createWindow(doc, { chrome });
  win.top = {};
  const ctx = h.loadScripts(ALL_FILES, win);
  t.after(() => ctx.__harness.dispose());
  assert.equal(chrome.runtime.sendMessage.calls.length, 0);
  const reply = await h.dispatchMessage(chrome.runtime.onMessage, {
    __emunium__: 2,
    id: 3,
    method: "waitForSelector",
    params: { selector: "#missing", timeoutMs: 0 },
  });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.code, "wait_timeout");
});

// ── focus ────────────────────────────────────────────────────────────────

test("focus and focusState report OS focus, active element and visibility", async (t) => {
  const { doc, send } = makeContent(t);
  button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  assert.deepEqual(await okResult(send("focusState", { elementId: id })), {
    documentHasFocus: true,
    activeIsElement: false,
    visibilityState: "visible",
  });
  assert.deepEqual(await okResult(send("focus", { elementId: id })), { focused: true });
  doc.__hasFocus = false;
  doc.visibilityState = "hidden";
  assert.deepEqual(await okResult(send("focusState", { elementId: id })), {
    documentHasFocus: false,
    activeIsElement: true,
    visibilityState: "hidden",
  });
  const blocked = button(doc, { id: "blocked" }, "Blocked");
  blocked.__focusable = false;
  const blockedId = (await okResult(send("querySelector", { selector: "#blocked" }))).elementId;
  assert.deepEqual(await okResult(send("focus", { elementId: blockedId })), { focused: false });
  assert.equal((await okResult(send("getAttribute", { elementId: id, name: "id" }))).value, "go");
  assert.equal(await errorCode(send("getAttribute", { elementId: id })), "invalid_argument");
});

// ── pointerProbe ─────────────────────────────────────────────────────────

test("pointerProbe: a trusted sample over the element is a hit", async (t) => {
  const { doc, win, send } = makeContent(t);
  const go = button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  move(go, { clientX: 50, clientY: 35, screenX: 150, screenY: 135 });
  const result = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 10, waitMs: 200 }));
  assert.equal(result.hit, true);
  assert.equal(result.event.clientX, 50);
  assert.equal(result.event.clientY, 35);
  assert.equal(result.event.screenX, 150);
  assert.equal(result.event.screenY, 135);
  assert.ok(result.event.time >= since);
  assert.deepEqual(result.rect, { x: 10, y: 20, width: 80, height: 30 });
  assert.equal(result.geometry.innerWidth, 1280);

  move(doc.body, { clientX: 500, clientY: 500, screenX: 600, screenY: 600 });
  const miss = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 10, waitMs: 200 }));
  assert.equal(miss.hit, false);
  assert.equal(miss.event.clientX, 500);
});

test("pointerProbe: no sample (none, stale or untrusted) gives event null within waitMs", async (t) => {
  const { doc, win, send } = makeContent(t);
  const go = button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  move(go, { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  await sleep(5);
  const since = Date.now();
  await sleep(2);
  go.dispatchEvent(new h.FakeMouseEvent("mousemove", { bubbles: true, isTrusted: false, clientX: 50, clientY: 35 }));
  const started = Date.now();
  const result = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 10, waitMs: 40 }));
  const elapsed = Date.now() - started;
  assert.deepEqual(
    { event: result.event, hit: result.hit },
    { event: null, hit: false }
  );
  assert.ok(result.rect && result.geometry);
  assert.ok(elapsed >= 35 && elapsed < 500, `elapsed ${elapsed}`);
  assert.equal(await errorCode(send("pointerProbe", { elementId: id, since, waitMs: -1 })), "invalid_argument");
  assert.equal(await errorCode(send("pointerProbe", { elementId: "zz.1", since })), "stale_element");
});

test("pointerProbe waits for a late sample, then for quietMs without newer samples", async (t) => {
  const { doc, win, send } = makeContent(t);
  const go = button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  const started = Date.now();
  const probe = send("pointerProbe", { elementId: id, since, quietMs: 60, waitMs: 1000 });
  setTimeout(() => move(go, { clientX: 20, clientY: 25, screenX: 20, screenY: 25 }), 20);
  setTimeout(() => move(go, { clientX: 60, clientY: 40, screenX: 60, screenY: 40 }), 50);
  const result = await okResult(probe);
  const elapsed = Date.now() - started;
  assert.equal(result.event.clientX, 60, "the newest sample after the quiet period wins");
  assert.equal(result.hit, true);
  assert.ok(elapsed >= 100, `returned before the quiet period: ${elapsed} ms`);
  assert.ok(elapsed < 900, `waited for the whole waitMs: ${elapsed} ms`);
});

test("pointerProbe unwraps shadow hosts for the hit test", async (t) => {
  const { doc, send } = makeContent(t);
  const host = button(doc, { id: "host" }, "");
  const other = button(doc, { id: "other" }, "", { x: 300, y: 20, width: 80, height: 30 });
  const shadow = { nodeType: 11, host };
  const inner = { nodeType: 1, parentNode: shadow, shadowRoot: null };
  host.shadowRoot = { elementFromPoint: () => inner };
  doc.setElementFromPoint(host);
  const hostId = (await okResult(send("querySelector", { selector: "#host" }))).elementId;
  const otherId = (await okResult(send("querySelector", { selector: "#other" }))).elementId;
  move(host, { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  assert.equal((await okResult(send("pointerProbe", { elementId: hostId, since: 0, quietMs: 5 }))).hit, true);
  assert.equal((await okResult(send("pointerProbe", { elementId: otherId, since: 0, quietMs: 5 }))).hit, false);
});

function frameScene(t) {
  const scene = makeContent(t);
  const go = button(scene.doc);
  const frame = h.el(scene.doc, "iframe", { id: "ad", src: "https://ads.example/" });
  frame.setRect({ x: 90, y: 0, width: 400, height: 300 });
  scene.doc.body.append(frame);
  return Object.assign(scene, { go, frame });
}

function leave(target, type, init) {
  const Event = type.startsWith("pointer") ? h.FakePointerEvent : h.FakeMouseEvent;
  target.dispatchEvent(new Event(type, Object.assign({ bubbles: true, isTrusted: true }, init)));
}

test("pointerProbe: a path across the button that exits into an iframe is not a hit", async (t) => {
  const { go, frame, send } = frameScene(t);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  move(go, { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  leave(go, "mouseout", { clientX: 89, clientY: 35, screenX: 89, screenY: 35, relatedTarget: frame, isTrusted: false });
  const before = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(before.hit, true, "untrusted mouseout is ignored");
  leave(go, "pointerout", { clientX: 89, clientY: 35, screenX: 89, screenY: 35, relatedTarget: frame });
  const result = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(result.hit, false);
  assert.equal(result.event.left, true);
  assert.equal(result.event.clientX, 89);
  // leaving one element for another inside the document is not a "left" sample
  move(go, { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  leave(go, "pointerout", { clientX: 50, clientY: 35, screenX: 50, screenY: 35, relatedTarget: go.parentNode });
  const inside = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(inside.hit, true);
  assert.equal(inside.event.left, undefined);
});

test("pointerProbe: a path that exits the window (relatedTarget null) is not a hit", async (t) => {
  const { go, send } = frameScene(t);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  move(go, { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  leave(go, "mouseout", { clientX: 50, clientY: 0, screenX: 50, screenY: 0, relatedTarget: null });
  const result = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(result.hit, false);
  assert.deepEqual(result.event, { clientX: 50, clientY: 0, screenX: 50, screenY: 0, time: result.event.time, left: true });
});

test("pointerProbe: a sample over a frame element is a hit only for that frame", async (t) => {
  const { doc, frame, send } = frameScene(t);
  const goId = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const frameId = (await okResult(send("querySelector", { selector: "#ad" }))).elementId;
  const wrapper = doc.body;
  const wrapperId = (await okResult(send("querySelector", { selector: "body" }))).elementId;
  move(frame, { clientX: 200, clientY: 100, screenX: 200, screenY: 100 });
  assert.equal((await okResult(send("pointerProbe", { elementId: goId, since: 0, quietMs: 5 }))).hit, false);
  assert.equal((await okResult(send("pointerProbe", { elementId: wrapperId, since: 0, quietMs: 5 }))).hit, false);
  assert.equal((await okResult(send("pointerProbe", { elementId: frameId, since: 0, quietMs: 5 }))).hit, true);
  assert.ok(wrapper.contains(frame));
});

// DragEvent is a MouseEvent; Chrome sends no pointermove/mousemove during a native drag.
function drag(target, type, init) {
  target.dispatchEvent(new h.FakeMouseEvent(type, Object.assign({ bubbles: true, isTrusted: true }, init)));
}

test("pointerProbe: trusted dragover samples stand in for pointermove during a drag", async (t) => {
  const { doc, win, send } = makeContent(t);
  const go = button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  const probe = send("pointerProbe", { elementId: id, since, quietMs: 10, waitMs: 1000 });
  setTimeout(() => drag(go, "dragover", { clientX: 50, clientY: 35, screenX: 150, screenY: 135 }), 20);
  const result = await okResult(probe);
  assert.equal(win.__listeners.get("pointermove").length, 1, "pointer listeners stay installed");
  assert.equal(result.hit, true);
  assert.deepEqual(result.event, {
    clientX: 50,
    clientY: 35,
    screenX: 150,
    screenY: 135,
    time: result.event.time,
    type: "dragover",
  });
  assert.ok(result.event.time >= since);

  drag(doc.body, "dragover", { clientX: 500, clientY: 500, screenX: 600, screenY: 600 });
  const miss = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(miss.hit, false);
  assert.equal(miss.event.clientX, 500);
  assert.equal(miss.event.type, "dragover");
});

test("pointerProbe: untrusted dragover is not a sample", async (t) => {
  const { doc, send } = makeContent(t);
  const go = button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  drag(go, "dragover", { clientX: 50, clientY: 35, screenX: 50, screenY: 35, isTrusted: false });
  const result = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5, waitMs: 40 }));
  assert.deepEqual({ event: result.event, hit: result.hit }, { event: null, hit: false });
});

test("pointerProbe: dragleave into a frame or out of the window is a left sample", async (t) => {
  const { go, frame, send } = frameScene(t);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  drag(go, "dragover", { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  drag(go, "dragleave", { clientX: 89, clientY: 35, screenX: 89, screenY: 35, relatedTarget: frame, isTrusted: false });
  assert.equal((await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }))).hit, true);
  drag(go, "dragleave", { clientX: 89, clientY: 35, screenX: 89, screenY: 35, relatedTarget: frame });
  const intoFrame = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(intoFrame.hit, false);
  assert.equal(intoFrame.event.left, true);
  assert.equal(intoFrame.event.clientX, 89);

  // leaving the button for its parent keeps the dragover sample
  drag(go, "dragover", { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  drag(go, "dragleave", { clientX: 50, clientY: 35, screenX: 50, screenY: 35, relatedTarget: go.parentNode });
  const inside = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(inside.hit, true);
  assert.equal(inside.event.left, undefined);

  drag(go, "dragleave", { clientX: 50, clientY: 0, screenX: 50, screenY: 0, relatedTarget: null });
  const outside = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5 }));
  assert.equal(outside.hit, false);
  assert.deepEqual(outside.event, { clientX: 50, clientY: 0, screenX: 50, screenY: 0, time: outside.event.time, left: true });
});

test("pointerProbe times samples on the wall clock even when performance.timeOrigin is skewed", async (t) => {
  const skewed = { timeOrigin: performance.timeOrigin - 120000, now: () => performance.now() };
  const { doc, send } = makeContent(t, { overrides: { performance: skewed } });
  const go = button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  const since = Date.now();
  await sleep(2);
  move(go, { clientX: 50, clientY: 35, screenX: 50, screenY: 35 });
  const after = Date.now();
  const result = await okResult(send("pointerProbe", { elementId: id, since, quietMs: 5, waitMs: 100 }));
  assert.notEqual(result.event, null, "a sample taken after `since` must count");
  assert.ok(result.event.time >= since && result.event.time <= after + 1, `time ${result.event.time}`);
  assert.equal(result.hit, true);
});

test("pointerProbe requires a numeric since", async (t) => {
  const { doc, send } = makeContent(t);
  button(doc);
  const id = (await okResult(send("querySelector", { selector: "#go" }))).elementId;
  assert.equal(await errorCode(send("pointerProbe", { elementId: id })), "invalid_argument");
  assert.equal(await errorCode(send("pointerProbe", { elementId: id, since: "1700000000000" })), "invalid_argument");
  assert.equal(await errorCode(send("pointerProbe", { elementId: id, since: null })), "invalid_argument");
});

test("hit and focus tests follow assignedSlot: slotted content is inside the shadow wrapper", async (t) => {
  const { doc, send } = makeContent(t);
  // card stands for the element inside the host's shadow tree that holds the <slot>
  const slot = h.el(doc, "slot", {});
  const card = h.el(doc, "div", { id: "card" }, [slot]);
  card.setRect({ x: 0, y: 0, width: 200, height: 100 });
  const label = h.el(doc, "span", { id: "label" }, "Pay");
  const host = h.el(doc, "x-pay", { id: "host" }, [label]);
  doc.body.append(card, host);
  label.assignedSlot = slot;
  doc.setElementFromPoint(label);
  const cardId = (await okResult(send("querySelector", { selector: "#card" }))).elementId;
  move(label, { clientX: 20, clientY: 20, screenX: 20, screenY: 20 });
  assert.equal((await okResult(send("pointerProbe", { elementId: cardId, since: 0, quietMs: 5 }))).hit, true);
  label.focus();
  assert.equal((await okResult(send("focusState", { elementId: cardId }))).activeIsElement, true);
});

// ── review fixes: identity, text cost, null documents ────────────────────

test("an element owned by another document is stale_element", async (t) => {
  const { scope, send } = makeContent(t);
  const otherDoc = h.createDocument({ url: "https://example.test/other" });
  h.createWindow(otherDoc);
  const foreign = h.el(otherDoc, "button", { id: "x" }, "X");
  otherDoc.body.append(foreign);
  const id = scope.getElementId(foreign);
  assert.equal(await errorCode(send("describeElement", { elementId: id })), "stale_element");
});

function countInnerText(element, counter) {
  Object.defineProperty(element, "innerText", {
    configurable: true,
    get() {
      counter.count += 1;
      return element.textContent;
    },
  });
}

test("findElementsByText prunes subtrees without the text and reads innerText only on candidates", (t) => {
  const { doc, scope } = makeContent(t);
  const pruned = { count: 0 };
  const deep = h.el(doc, "p", {}, "nothing to see");
  const big = h.el(doc, "section", {}, [deep, h.el(doc, "p", {}, "more filler")]);
  countInnerText(big, pruned);
  countInnerText(deep, pruned);
  const caps = h.el(doc, "button", { id: "caps" }, "Sign  in");
  caps.__innerText = "SIGN IN"; // CSS text-transform: uppercase
  doc.body.append(big, h.el(doc, "div", {}, [caps]));
  assert.deepEqual(Array.from(scope.findElementsByText("sign in", false)), [caps]);
  assert.deepEqual(Array.from(scope.findElementsByText("SIGN IN", true)), [caps]);
  assert.equal(pruned.count, 0, "innerText read on a subtree that cannot match");
});

test("getElementText uses a textContent prefix for huge containers", async (t) => {
  const { doc, send } = makeContent(t);
  const reads = { count: 0 };
  const huge = h.el(doc, "main", { id: "huge" }, "x".repeat(6000));
  countInnerText(huge, reads);
  const small = h.el(doc, "p", { id: "small" }, "y");
  small.__innerText = "rendered y";
  doc.body.append(huge, small);
  const payload = await okResult(send("querySelector", { selector: "#huge" }));
  assert.equal(payload.text, "x".repeat(500));
  assert.equal(reads.count, 0);
  assert.equal((await okResult(send("querySelector", { selector: "#small" }))).text, "rendered y");
});

test("pageInfo and text queries survive a null documentElement or body", async (t) => {
  const { doc, send } = makeContent(t);
  doc.documentElement = null;
  doc.body = null;
  const info = await okResult(send("pageInfo"));
  assert.equal(info.scrollWidth, 0);
  assert.equal(info.scrollHeight, 0);
  assert.equal(info.geometry.innerWidth, 1280);
  for (const key of ["scrollWidth", "scrollHeight", "clientWidth", "clientHeight"]) {
    assert.equal(info.geometry[key], 0, key);
  }
  assert.deepEqual(await okResult(send("queryByText", { text: "x" })), []);
});
