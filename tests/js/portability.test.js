"use strict";
// tests/js/portability.test.js - extension runtime differences between Chromium and Firefox:
// importScripts-less background page, empty-body packaged files, closed shadow roots via
// element.openOrClosedShadowRoot, and NS_BINDING_ABORTED during a navigation that still loads.
// Run: node --test tests/js/portability.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const h = require("./harness");

const SESSION = { protocol: 2, port: 4321, token: "tok-abc" };
const BACKGROUND_SIBLINGS = [
  "background_state.js",
  "background_network.js",
  "background_handlers.js",
  "background_runtime.js",
];
const CONTENT_FILES = ["content_shared.js", "content_queries.js", "content.js"];
const NS_BINDING_ABORTED = "Error code 2152398850";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const plain = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

async function until(predicate, ms = 1500) {
  const end = Date.now() + ms;
  for (;;) {
    const value = predicate();
    if (value) {
      return value;
    }
    if (Date.now() > end) {
      throw new Error("condition not met within " + ms + " ms");
    }
    await sleep(2);
  }
}

// ── background worker helpers (local copies of background.test.js) ──────

function makeWorker(t, { files, globals = {}, preload = [] } = {}) {
  h.FakeWebSocket.reset();
  const chrome = h.createFakeChrome({ files: files === undefined ? { "session.json": SESSION } : files });
  // The globals object itself becomes the context, so callers can reach ctx.__harness.
  const sandbox = Object.assign(globals, {
    chrome,
    WebSocket: h.FakeWebSocket,
    navigator: { userAgent: "test-agent/1.0" },
  });
  const ctx = h.loadScripts(preload.concat(["background.js"]), sandbox);
  const bg = ctx.EmuniumBackground;
  bg.waitForResponse = h.spy(async () => null);
  bg.getRecentResponses = h.spy(async () => []);
  t.after(() => ctx.__harness.dispose());
  return { chrome, ctx, bg };
}

async function welcomed() {
  const ws = await until(() => h.FakeWebSocket.last());
  ws.open();
  ws.serverSend({ type: "welcome", protocol: 2 });
  return ws;
}

let nextId = 1;
async function request(ws, method, params = {}, ms) {
  const id = nextId++;
  ws.serverSend({ type: "request", id, method, params, target: { tabId: null, frameId: 0 } });
  return until(() => ws.sent.find((f) => f && f.type === "reply" && f.id === id), ms);
}

function scriptUpdate(w, script) {
  w.baseUpdate = w.baseUpdate || w.chrome.tabs.update;
  const update = w.baseUpdate;
  w.chrome.tabs.update = h.spy(async (tabId, props) => {
    const tab = await update(tabId, props);
    script(w.chrome.webNavigation, tabId, props);
    return tab;
  });
}

// ── 1. background.js without importScripts ─────────────────────────────

test("background.js runs without importScripts when the siblings are already loaded", async (t) => {
  // `importScripts: undefined` keeps the harness from installing its own.
  const w = makeWorker(t, { globals: { importScripts: undefined }, preload: BACKGROUND_SIBLINGS });
  assert.equal(typeof w.ctx.importScripts, "undefined");
  assert.equal(typeof w.bg.connect, "function");
  const ws = await until(() => h.FakeWebSocket.last());
  assert.equal(ws.url, "ws://127.0.0.1:4321");
});

test("background.js still loads its siblings through importScripts when it exists", async (t) => {
  const imported = [];
  const sandbox = {};
  sandbox.importScripts = (...names) => {
    imported.push(...names);
    for (const name of names) {
      const file = path.join(sandbox.__harness.baseDir, name);
      sandbox.__harness.evaluate(fs.readFileSync(file, "utf8"), file);
    }
  };
  const w = makeWorker(t, { globals: sandbox });
  assert.deepEqual(imported, BACKGROUND_SIBLINGS);
  assert.equal(typeof w.bg.connect, "function");
});

// ── 2. readPackagedJson: empty body is a missing file ───────────────────

test("an empty or whitespace attach.json (Firefox 200 for a missing file) is absent: session.json is used", async (t) => {
  for (const body of ["", "  \r\n\t "]) {
    const w = makeWorker(t, { files: { "session.json": SESSION, "attach.json": body } });
    const config = plain(await w.bg.loadConfig());
    assert.ok(config, `config for body ${JSON.stringify(body)}`);
    assert.equal(config.url, "ws://127.0.0.1:4321");
    assert.equal(config.token, "tok-abc");
  }
});

test("a non-empty unparsable attach.json is still present (invalid, retried, never bypassed)", async (t) => {
  const w = makeWorker(t, { files: { "session.json": SESSION, "attach.json": "{not json" } });
  assert.equal(await w.bg.loadConfig(), null);
});

test("an empty session.json alone is absent: no config", async (t) => {
  const w = makeWorker(t, { files: { "session.json": "" } });
  assert.equal(await w.bg.loadConfig(), null);
});

// ── 3. closed shadow roots ─────────────────────────────────────────────

function makeContent(t, { chromeDom } = {}) {
  const document = h.createDocument({ url: "https://example.test/page", title: "Page" });
  const chrome = h.createFakeChrome();
  if (chromeDom) {
    chrome.dom = chromeDom;
  }
  const win = h.createWindow(document, { chrome });
  win.top = win;
  win.EmuniumContent = { waitForSelector: async () => ({ element: null, matched: true, count: 0 }) };
  const ctx = h.loadScripts(CONTENT_FILES, win);
  t.after(() => ctx.__harness.dispose());
  const send = (method, params = {}) =>
    h.dispatchMessage(chrome.runtime.onMessage, { __emunium__: 2, id: 1, method, params }).then(plain);
  return { doc: document, send };
}

// The host holds focus at document level; its shadow root's activeElement lives inside #card.
// focusState(#card) is true only when deepActiveElement descended through that root.
function shadowFixture(doc) {
  const inner = h.el(doc, "input", { id: "inner" });
  const card = h.el(doc, "div", { id: "card" }, [inner]);
  const host = h.el(doc, "button", { id: "host" }, "Host");
  doc.body.append(card, host);
  host.focus();
  assert.equal(doc.activeElement, host);
  const root = { nodeType: 11, host, activeElement: inner };
  return { host, root };
}

async function cardIsActive(send) {
  const found = await send("querySelector", { selector: "#card" });
  assert.equal(found.ok, true, JSON.stringify(found));
  const state = await send("focusState", { elementId: found.result.elementId });
  assert.equal(state.ok, true, JSON.stringify(state));
  return state.result.activeIsElement;
}

// Firefox content scripts: Element.openOrClosedShadowRoot is a read-only property, not a method.
function defineClosedRootProperty(host, value) {
  Object.defineProperty(host, "openOrClosedShadowRoot", { get: () => value, configurable: true });
}

test("closed shadow root: element.openOrClosedShadowRoot property when chrome.dom is absent (Firefox)", async (t) => {
  const { doc, send } = makeContent(t);
  const { host, root } = shadowFixture(doc);
  assert.equal(await cardIsActive(send), false, "without any root the host is the active element");
  defineClosedRootProperty(host, root);
  assert.equal(typeof host.openOrClosedShadowRoot, "object");
  assert.equal(await cardIsActive(send), true, "read-only getter property");

  const plainHost = makeContent(t);
  const plainFixture = shadowFixture(plainHost.doc);
  plainFixture.host.openOrClosedShadowRoot = plainFixture.root;
  assert.equal(await cardIsActive(plainHost.send), true, "plain data property");
});

test("closed shadow root: an openOrClosedShadowRoot property holding null falls back to shadowRoot", async (t) => {
  const { doc, send } = makeContent(t);
  const { host, root } = shadowFixture(doc);
  defineClosedRootProperty(host, null);
  assert.equal(await cardIsActive(send), false, "null property and no shadowRoot: the host stays active");
  host.shadowRoot = root;
  assert.equal(await cardIsActive(send), true);
});

test("closed shadow root: chrome.dom.openOrClosedShadowRoot is preferred when available (Chromium)", async (t) => {
  const seen = [];
  let domRoot = null;
  const { doc, send } = makeContent(t, {
    chromeDom: {
      openOrClosedShadowRoot: (element) => {
        seen.push(element);
        return element === domRoot?.host ? domRoot : null;
      },
    },
  });
  const { host, root } = shadowFixture(doc);
  let elementReads = 0;
  Object.defineProperty(host, "openOrClosedShadowRoot", {
    get: () => {
      elementReads += 1;
      return null;
    },
    configurable: true,
  });
  domRoot = root;
  assert.equal(await cardIsActive(send), true);
  assert.ok(seen.includes(host));
  assert.equal(elementReads, 0, "the element property is not read when chrome.dom answers");
});

test("closed shadow root: falls back to element.shadowRoot when neither API is available", async (t) => {
  const { doc, send } = makeContent(t);
  const { host, root } = shadowFixture(doc);
  host.shadowRoot = root;
  assert.equal(await cardIsActive(send), true);
});

// ── 4. NS_BINDING_ABORTED during a navigation that still loads ──────────

test("navigate: NS_BINDING_ABORTED followed by commit and load succeeds", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  for (const error of [NS_BINDING_ABORTED, "NS_BINDING_ABORTED"]) {
    for (const waitUntil of ["load", "commit"]) {
      scriptUpdate(w, (nav, tabId, props) => {
        nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
        nav.onErrorOccurred.emit({ tabId, frameId: 0, url: props.url, error });
        setTimeout(() => {
          nav.onCommitted.emit({ tabId, frameId: 0, url: props.url });
          nav.onDOMContentLoaded.emit({ tabId, frameId: 0, url: props.url });
          nav.onCompleted.emit({ tabId, frameId: 0, url: props.url });
        }, 15);
      });
      const reply = await request(ws, "navigate", { url: "https://firefox.test/", waitUntil, timeoutMs: 1000 });
      assert.equal(reply.ok, true, `${error} / ${waitUntil}: ${JSON.stringify(reply)}`);
    }
  }
});

test("navigate: NS_BINDING_ABORTED after commit, then load, succeeds", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  scriptUpdate(w, (nav, tabId, props) => {
    nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
    nav.onCommitted.emit({ tabId, frameId: 0, url: props.url });
    nav.onErrorOccurred.emit({ tabId, frameId: 0, url: props.url, error: NS_BINDING_ABORTED });
    setTimeout(() => nav.onCompleted.emit({ tabId, frameId: 0, url: props.url }), 15);
  });
  const reply = await request(ws, "navigate", { url: "https://firefox.test/late", timeoutMs: 1000 });
  assert.equal(reply.ok, true, JSON.stringify(reply));
});

test("navigate: NS_BINDING_ABORTED with nothing after it fails as navigation_failed at the timeout", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  scriptUpdate(w, (nav, tabId, props) => {
    nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
    nav.onErrorOccurred.emit({ tabId, frameId: 0, url: props.url, error: NS_BINDING_ABORTED });
  });
  const reply = await request(ws, "navigate", { url: "https://aborted.test/", timeoutMs: 40 });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.code, "navigation_failed");
  assert.match(reply.error.message, /aborted\.test.*2152398850/);
});

test("navigate: a plain error still fails at once, even when a commit follows", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  for (const error of ["Error code 2152398878", "net::ERR_NAME_NOT_RESOLVED"]) {
    scriptUpdate(w, (nav, tabId, props) => {
      nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
      nav.onErrorOccurred.emit({ tabId, frameId: 0, url: props.url, error });
      setTimeout(() => {
        nav.onCommitted.emit({ tabId, frameId: 0, url: props.url });
        nav.onCompleted.emit({ tabId, frameId: 0, url: props.url });
      }, 15);
    });
    const started = Date.now();
    const reply = await request(ws, "navigate", { url: "https://broken.test/", timeoutMs: 1000 });
    assert.equal(reply.ok, false, error);
    assert.equal(reply.error.code, "navigation_failed");
    assert.ok(reply.error.message.includes(error), reply.error.message);
    assert.ok(Date.now() - started < 500, "failed without waiting for the timeout");
  }
});

test("navigate: NS_BINDING_ABORTED then a real error fails with the real error", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  scriptUpdate(w, (nav, tabId, props) => {
    nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
    nav.onErrorOccurred.emit({ tabId, frameId: 0, url: props.url, error: NS_BINDING_ABORTED });
    nav.onErrorOccurred.emit({ tabId, frameId: 0, url: props.url, error: "Error code 2152398878" });
  });
  const reply = await request(ws, "navigate", { url: "https://broken.test/", timeoutMs: 1000 });
  assert.equal(reply.error.code, "navigation_failed");
  assert.match(reply.error.message, /2152398878/);
});
