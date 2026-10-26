"use strict";
// tests/js/background.test.js - protocol v2 service worker (emunium/extension/background*.js).
// Run: node --test tests/js/background.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const h = require("./harness");

const SESSION = { protocol: 2, port: 4321, token: "tok-abc" };
const CONTENT_FILES = [
  "content_shared.js",
  "content_queries.js",
  "content_conditions.js",
  "content_wait.js",
  "content.js",
];
const NO_RECEIVER = "Could not establish connection. Receiving end does not exist.";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// Objects created inside the vm realm have foreign prototypes; compare their JSON shape.
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

function makeWorker(t, { tabs, windows, files, before } = {}) {
  h.FakeWebSocket.reset();
  const chrome = h.createFakeChrome({
    tabs,
    windows,
    files: files === undefined ? { "session.json": SESSION } : files,
  });
  if (before) {
    before(chrome);
  }
  const globals = { chrome, WebSocket: h.FakeWebSocket, navigator: { userAgent: "test-agent/1.0" } };
  const ctx = h.loadScripts(["background.js"], globals);
  const bg = ctx.EmuniumBackground;
  // background_network.js is owned by S3: stub its two entry points.
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
function send(ws, method, params = {}, tabId = null) {
  const id = nextId++;
  ws.serverSend({ type: "request", id, method, params, target: { tabId, frameId: 0 } });
  return id;
}
const replyFor = (ws, id) => ws.sent.find((f) => f && f.type === "reply" && f.id === id);
async function request(ws, method, params = {}, tabId = null, ms) {
  const id = send(ws, method, params, tabId);
  return until(() => replyFor(ws, id), ms);
}
const events = (ws, name) => ws.sent.filter((f) => f && f.type === "event" && f.event === name);

// Runs an injection like Chrome does: the function is serialized and evaluated in a
// separate realm; `csp` disables string compilation (EvalError on new Function).
function pageExecutor(chrome, { csp = false, documentId = "doc-1-0" } = {}) {
  return h.makeApi(chrome, async (injection) => {
    if (injection.files) {
      return [{ frameId: 0, documentId, result: undefined }];
    }
    const page = vm.createContext({ setTimeout }, { codeGeneration: { strings: !csp, wasm: false } });
    const fn = vm.runInContext("(" + injection.func.toString() + ")", page);
    const result = await fn(...injection.args);
    return [{ frameId: 0, documentId, result }];
  });
}

// ── connection ───────────────────────────────────────────────────────────

test("handshake: hello first, nothing answered before welcome, keepalive only when welcomed", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.keepAliveMs = 15;
  const ws = await until(() => h.FakeWebSocket.last());
  assert.equal(ws.url, "ws://127.0.0.1:4321");
  const [url, init] = w.ctx.fetch.calls[0];
  assert.match(url, /^chrome-extension:\/\/fakeextensionid\/session\.json/);
  assert.equal(init.cache, "no-store");

  ws.open();
  assert.deepEqual(ws.sent, [
    { type: "hello", protocol: 2, token: "tok-abc", extensionVersion: "3.1.0", userAgent: "test-agent/1.0" },
  ]);
  ws.serverSend({ type: "request", id: 500, method: "getTabInfo", params: {}, target: { tabId: null, frameId: 0 } });
  await sleep(50);
  assert.equal(ws.sent.length, 1, "no reply and no ping before welcome");
  assert.equal(w.bg.isConnected(), false);

  ws.serverSend({ type: "welcome", protocol: 2 });
  assert.equal(w.bg.isConnected(), true);
  await until(() => events(ws, "ping").length > 0);
  const reply = await request(ws, "getTabInfo");
  assert.equal(reply.ok, true);
  assert.equal(reply.tabId, 1);

  ws.serverClose(1000);
  const pings = events(ws, "ping").length;
  await sleep(50);
  assert.equal(events(ws, "ping").length, pings, "keepalive stops with the connection");
  assert.equal(ws.droppedSent.length, 0);
});

test("handshake: a welcome with another protocol is rejected", async (t) => {
  makeWorker(t);
  const ws = await until(() => h.FakeWebSocket.last());
  ws.open();
  ws.serverSend({ type: "welcome", protocol: 1 });
  assert.equal(ws.closeCalls[0].code, 4000);
});

test("loadConfig reads session.json and retries every configRetryMs while it is missing", async (t) => {
  const w = makeWorker(t, { files: {} });
  w.bg.timing.configRetryMs = 20;
  assert.equal(await w.bg.loadConfig(), null);
  w.chrome.__state.files["session.json"] = { protocol: 1, port: 1, token: "x" };
  assert.equal(await w.bg.loadConfig(), null, "wrong protocol is invalid");
  w.chrome.__state.files["session.json"] = { protocol: 2, port: 99999, token: "x" };
  assert.equal(await w.bg.loadConfig(), null, "invalid port");
  assert.equal(h.FakeWebSocket.instances.length, 0);

  w.chrome.__state.files["session.json"] = SESSION;
  assert.deepEqual(plain(await w.bg.loadConfig()), { url: "ws://127.0.0.1:4321", token: "tok-abc", protocol: 2 });
  const ws = await until(() => h.FakeWebSocket.last());
  assert.equal(ws.url, "ws://127.0.0.1:4321");
});

test("duplicate-connect guard: one socket per in-flight connect, backoff 1 s -> 5 s", async (t) => {
  const w = makeWorker(t);
  await Promise.all([w.bg.connect(), w.bg.connect(), w.bg.connect()]);
  assert.equal(h.FakeWebSocket.instances.length, 1);
  const first = h.FakeWebSocket.last();
  assert.equal(first.readyState, h.FakeWebSocket.CONNECTING);
  await w.bg.connect();
  w.bg.ensureConnected();
  await h.tick();
  assert.equal(h.FakeWebSocket.instances.length, 1, "no second socket while CONNECTING");

  w.bg.resetBackoff();
  assert.deepEqual([1, 2, 3, 4, 5].map(() => w.bg.takeBackoff()), [1000, 2000, 4000, 5000, 5000]);

  w.bg.timing.backoffMinMs = 10;
  w.bg.timing.backoffMaxMs = 40;
  w.bg.resetBackoff();
  first.fail();
  w.bg.ensureConnected();
  await h.tick();
  assert.equal(h.FakeWebSocket.instances.length, 1, "the watchdog does not cut a pending backoff short");
  const second = await until(() => h.FakeWebSocket.instances[1]);
  second.open();
  second.serverSend({ type: "welcome", protocol: 2 });
  assert.equal(w.bg.state.backoffMs, 10, "welcome resets the backoff");
  await sleep(30);
  assert.equal(h.FakeWebSocket.instances.length, 2);
});

test("chrome.alarms: a 0.5-minute alarm reconnects a worker that is not welcomed", async (t) => {
  const w = makeWorker(t, {
    before: (chrome) => {
      chrome.alarms = { create: h.spy(async () => undefined), onAlarm: new h.ChromeEvent("alarms.onAlarm") };
    },
  });
  assert.deepEqual(plain(w.chrome.alarms.create.calls), [["emunium-reconnect", { periodInMinutes: 0.5 }]]);
  const ws = await welcomed();
  w.chrome.alarms.onAlarm.emit({ name: "emunium-reconnect" });
  await h.tick();
  assert.equal(h.FakeWebSocket.instances.length, 1, "no reconnect while welcomed");

  w.bg.timing.backoffMinMs = 60000;
  w.bg.timing.backoffMaxMs = 60000;
  w.bg.resetBackoff();
  ws.serverClose(1006);
  w.chrome.alarms.onAlarm.emit({ name: "something-else" });
  await h.tick();
  assert.equal(h.FakeWebSocket.instances.length, 1);
  w.chrome.alarms.onAlarm.emit({ name: "emunium-reconnect" });
  await until(() => h.FakeWebSocket.instances.length === 2);
});

test("chrome.alarms missing: the worker still loads and connects", async (t) => {
  const w = makeWorker(t, { before: (chrome) => delete chrome.alarms });
  assert.equal(w.chrome.alarms, undefined);
  await welcomed();
  assert.equal(w.bg.isConnected(), true);
});

// ── target resolution ────────────────────────────────────────────────────

test("target resolution: target.tabId, else active tab of the last focused normal window", async (t) => {
  const w = makeWorker(t, {
    windows: [
      { id: 1, type: "normal" },
      { id: 2, type: "popup", focused: true },
    ],
    tabs: [
      { id: 1, windowId: 1, active: true, url: "https://one.test/" },
      { id: 2, windowId: 1, active: false, url: "https://two.test/" },
      { id: 3, windowId: 2, active: true, url: "https://popup.test/" },
    ],
  });
  const ws = await welcomed();
  assert.equal((await request(ws, "getTabInfo")).result.tabId, 1, "popup window skipped");
  assert.equal((await request(ws, "getTabInfo", {}, 2)).result.tabId, 2);
  assert.equal((await request(ws, "getTabInfo", { tabId: 3 })).tabId, 1, "params.tabId is ignored");
  const closed = await request(ws, "getTabInfo", {}, 42);
  assert.equal(closed.ok, false);
  assert.equal(closed.error.code, "target_closed");
  assert.deepEqual(w.bg.state.readyTabs.size, 0);
  assert.equal("pinnedTabId" in w.bg.state, false, "the worker keeps no pin");
});

test("target resolution: picks the last focused of several normal windows; none -> no_target", async (t) => {
  const w = makeWorker(t, {
    windows: [
      { id: 1, type: "normal" },
      { id: 2, type: "normal", focused: true },
    ],
    tabs: [
      { id: 1, windowId: 1, active: true },
      { id: 5, windowId: 2, active: true, url: "https://five.test/", title: "Five" },
    ],
  });
  const ws = await welcomed();
  const info = await request(ws, "getTabInfo");
  assert.deepEqual(info.result, {
    tabId: 5,
    url: "https://five.test/",
    title: "Five",
    status: "complete",
    active: true,
    windowId: 2,
  });
  w.chrome.__state.tabs.clear();
  const none = await request(ws, "getTabInfo");
  assert.equal(none.error.code, "no_target");
  assert.equal(none.tabId, null);
});

// ── tabs ─────────────────────────────────────────────────────────────────

test("closeTab closes the target tab even when it is not active, and emits tabRemoved", async (t) => {
  const w = makeWorker(t, {
    tabs: [
      { id: 1, windowId: 1, active: true },
      { id: 2, windowId: 1, active: false },
    ],
  });
  const ws = await welcomed();
  const reply = await request(ws, "closeTab", {}, 2);
  assert.deepEqual(reply, { type: "reply", id: reply.id, ok: true, result: { closed: true }, tabId: 2 });
  assert.equal(w.chrome.__state.tabs.has(2), false);
  assert.equal(w.chrome.__state.tabs.has(1), true, "the active tab is untouched");
  assert.deepEqual(events(ws, "tabRemoved"), [{ type: "event", event: "tabRemoved", tabId: 2 }]);
  const again = await request(ws, "closeTab", {}, 2);
  assert.equal(again.error.code, "target_closed");
});

test("listTabs, activateTab and createTab", async (t) => {
  const w = makeWorker(t, {
    tabs: [
      { id: 1, windowId: 1, active: true, url: "https://one.test/", title: "One" },
      { id: 2, windowId: 1, active: false, url: "https://two.test/", title: "Two" },
    ],
  });
  const ws = await welcomed();
  assert.deepEqual((await request(ws, "listTabs")).result, [
    { tabId: 1, url: "https://one.test/", title: "One", active: true, windowId: 1 },
    { tabId: 2, url: "https://two.test/", title: "Two", active: false, windowId: 1 },
  ]);
  const activated = await request(ws, "activateTab", {}, 2);
  assert.deepEqual(activated.result, { tabId: 2, windowId: 1, activated: true });
  assert.equal(w.chrome.__state.tabs.get(1).active, false);
  assert.equal(w.chrome.windows.update.calls.length, 1);

  const create = w.chrome.tabs.create;
  w.chrome.tabs.create = h.spy(async (props) => {
    const tab = await create(props);
    // Fires before create() resolves: the watcher buffers events for the unknown tab id.
    w.chrome.webNavigation.onBeforeNavigate.emit({ tabId: tab.id, frameId: 0, url: props.url });
    w.chrome.webNavigation.onCommitted.emit({ tabId: tab.id, frameId: 0, url: props.url });
    w.chrome.__state.tabs.get(tab.id).title = "New";
    w.chrome.webNavigation.onCompleted.emit({ tabId: tab.id, frameId: 0, url: props.url });
    return tab;
  });
  const created = await request(ws, "createTab", { url: "https://new.test/", active: false, timeoutMs: 500 });
  assert.equal(created.ok, true, JSON.stringify(created));
  assert.deepEqual(created.result, { tabId: 3, url: "https://new.test/", title: "New", contentReady: false });
  assert.equal(created.tabId, 3);
  assert.equal(plain(w.chrome.tabs.create.calls[0][0]).active, false);

  w.chrome.tabs.create = create;
  const timedOut = await request(ws, "createTab", { url: "https://slow.test/", timeoutMs: 30 });
  assert.equal(timedOut.error.code, "navigation_timeout");
  assert.equal(timedOut.tabId, 4);
});

// ── navigate ─────────────────────────────────────────────────────────────

// Replaces tabs.update with the fake default plus `script` (never chains earlier scripts).
function scriptUpdate(w, script) {
  w.baseUpdate = w.baseUpdate || w.chrome.tabs.update;
  const update = w.baseUpdate;
  w.chrome.tabs.update = h.spy(async (tabId, props) => {
    const tab = await update(tabId, props);
    script(w.chrome.webNavigation, tabId, props);
    return tab;
  });
}

test("navigate success: listeners attached before tabs.update, waits for load, contentReady", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  let listenersAtUpdate = null;
  scriptUpdate(w, (nav, tabId, props) => {
    listenersAtUpdate = nav.onCompleted.hasListeners();
    // Synchronous emits before update() resolves: lost if listeners were attached later.
    nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
    nav.onCommitted.emit({ tabId, frameId: 0, url: props.url, documentId: "doc-1-0" });
    h.dispatchMessage(
      w.chrome.runtime.onMessage,
      { __emunium_ready__: 2, url: props.url },
      { tab: { id: tabId }, frameId: 0, documentId: "doc-1-0" }
    );
    nav.onCommitted.emit({ tabId, frameId: 3, url: "https://frame.test/" });
    nav.onDOMContentLoaded.emit({ tabId, frameId: 0, url: props.url });
    w.chrome.__state.tabs.get(tabId).title = "Next";
    nav.onCompleted.emit({ tabId, frameId: 0, url: props.url });
  });
  const reply = await request(ws, "navigate", { url: "https://next.test/", waitUntil: "load", timeoutMs: 500 });
  assert.equal(listenersAtUpdate, true);
  assert.deepEqual(reply, {
    type: "reply",
    id: reply.id,
    ok: true,
    result: { url: "https://next.test/", title: "Next", tabId: 1, contentReady: true },
    tabId: 1,
  });
  assert.deepEqual(events(ws, "tabReady"), [{ type: "event", event: "tabReady", tabId: 1, url: "https://next.test/" }]);
  assert.equal(w.chrome.webNavigation.onCompleted.hasListeners(), false, "watcher removed its listeners");
});

test("navigate commit: stale readiness of the previous document is not reported", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  await h.dispatchMessage(
    w.chrome.runtime.onMessage,
    { __emunium_ready__: 2, url: "https://example.test/" },
    { tab: { id: 1 }, frameId: 0, documentId: "doc-old" }
  );
  scriptUpdate(w, (nav, tabId, props) => {
    nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
    nav.onCommitted.emit({ tabId, frameId: 0, url: props.url, documentId: "doc-1-0" });
  });
  const reply = await request(ws, "navigate", { url: "https://next.test/", waitUntil: "commit", timeoutMs: 500 });
  assert.equal(reply.ok, true);
  assert.equal(reply.result.contentReady, false);
});

test("navigate: fragment and history navigations complete", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  scriptUpdate(w, (nav, tabId, props) => {
    nav.onReferenceFragmentUpdated.emit({ tabId, frameId: 0, url: props.url });
  });
  const fragment = await request(ws, "navigate", { url: "https://example.test/#part", timeoutMs: 500 });
  assert.equal(fragment.ok, true);
  scriptUpdate(w, (nav, tabId, props) => {
    nav.onHistoryStateUpdated.emit({ tabId, frameId: 0, url: props.url });
  });
  const history = await request(ws, "navigate", { url: "https://example.test/route", timeoutMs: 500 });
  assert.equal(history.ok, true);
});

test("navigate timeout is an error, and a late onCompleted of the old page does not count", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const before = w.chrome.webNavigation.onCommitted.listeners.length;
  scriptUpdate(w, (nav, tabId) => {
    nav.onCompleted.emit({ tabId, frameId: 0, url: "https://example.test/" });
  });
  const started = Date.now();
  const reply = await request(ws, "navigate", { url: "https://slow.test/", timeoutMs: 40 });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.code, "navigation_timeout");
  assert.match(reply.error.message, /slow\.test.*load.*40 ms/);
  assert.equal(reply.tabId, 1);
  assert.ok(Date.now() - started < 1000);
  assert.equal(w.chrome.webNavigation.onCommitted.listeners.length, before);
  assert.equal(w.chrome.tabs.onUpdated.hasListeners(), false);
});

test("navigate error: navigation_failed, superseded aborts ignored, closed tab -> target_closed", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  scriptUpdate(w, (nav, tabId, props) => {
    nav.onErrorOccurred.emit({ tabId, frameId: 0, url: "https://previous.test/", error: "net::ERR_ABORTED" });
    nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
    nav.onErrorOccurred.emit({ tabId, frameId: 0, url: props.url, error: "net::ERR_NAME_NOT_RESOLVED" });
  });
  const failed = await request(ws, "navigate", { url: "https://nowhere.invalid/", timeoutMs: 500 });
  assert.equal(failed.ok, false);
  assert.equal(failed.error.code, "navigation_failed");
  assert.match(failed.error.message, /ERR_NAME_NOT_RESOLVED/);

  scriptUpdate(w, (nav, tabId) => {
    setTimeout(() => w.chrome.tabs.remove(tabId), 5);
  });
  const closed = await request(ws, "navigate", { url: "https://next.test/", timeoutMs: 500 });
  assert.equal(closed.error.code, "target_closed");

  const bad = await request(ws, "navigate", { url: "https://x.test/", waitUntil: "networkidle" }, null);
  assert.equal(bad.error.code, "invalid_argument");
  const noUrl = await request(ws, "navigate", {});
  assert.equal(noUrl.error.code, "invalid_argument");
});

test("navigate: the old page's replaceState or commit before our onBeforeNavigate does not end the wait", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  for (const waitUntil of ["load", "commit"]) {
    scriptUpdate(w, (nav, tabId, props) => {
      // Old document: history.replaceState and a commit of its own pending navigation.
      nav.onHistoryStateUpdated.emit({ tabId, frameId: 0, url: "https://example.test/?state=2" });
      nav.onCommitted.emit({ tabId, frameId: 0, url: "https://example.test/old" });
      nav.onCompleted.emit({ tabId, frameId: 0, url: "https://example.test/old" });
      setTimeout(() => {
        w.chrome.__state.tabs.get(tabId).title = "Ours " + waitUntil;
        nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
        nav.onCommitted.emit({ tabId, frameId: 0, url: props.url });
        nav.onCompleted.emit({ tabId, frameId: 0, url: props.url });
      }, 30);
    });
    const reply = await request(ws, "navigate", { url: "https://next.test/", waitUntil, timeoutMs: 1000 });
    assert.equal(reply.ok, true, JSON.stringify(reply));
    assert.equal(reply.result.title, "Ours " + waitUntil, "finished only after our own navigation");
  }
  scriptUpdate(w, (nav, tabId) => {
    nav.onReferenceFragmentUpdated.emit({ tabId, frameId: 0, url: "https://example.test/#other" });
  });
  const wrongFragment = await request(ws, "navigate", { url: "https://example.test/#wanted", timeoutMs: 40 });
  assert.equal(wrongFragment.error.code, "navigation_timeout");
});

test("navigate: ERR_ABORTED on a main-frame redirect target is our failure (download)", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const redirectListeners = w.chrome.webRequest.onBeforeRedirect.listeners.length;
  scriptUpdate(w, (nav, tabId, props) => {
    const entries = w.chrome.webRequest.onBeforeRedirect.entries;
    const redirect = entries[entries.length - 1];
    assert.deepEqual(plain(redirect.extra), [{ urls: ["<all_urls>"], types: ["main_frame"] }]);
    nav.onBeforeNavigate.emit({ tabId, frameId: 0, url: props.url });
    w.chrome.webRequest.onBeforeRedirect.emit({
      tabId,
      frameId: 0,
      type: "main_frame",
      url: props.url,
      redirectUrl: "https://cdn.test/file.zip",
    });
    nav.onErrorOccurred.emit({ tabId, frameId: 0, url: "https://cdn.test/file.zip", error: "net::ERR_ABORTED" });
  });
  const reply = await request(ws, "navigate", { url: "https://get.test/download", timeoutMs: 500 });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.code, "navigation_failed");
  assert.match(reply.error.message, /cdn\.test\/file\.zip.*ERR_ABORTED/);
  assert.equal(w.chrome.webRequest.onBeforeRedirect.listeners.length, redirectListeners, "redirect listener removed");
});

test("navigate/createTab: other rejections map to no_target or internal", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.tabs.create = h.spy(async () => {
    throw new Error("No current window");
  });
  assert.equal((await request(ws, "createTab", { url: "https://a.test/" })).error.code, "no_target");
  w.chrome.tabs.update = h.spy(async () => {
    throw new Error("Tabs cannot be edited right now (user may be dragging a tab).");
  });
  const busy = await request(ws, "navigate", { url: "https://a.test/" });
  assert.deepEqual(busy.error, { code: "internal", message: "Tabs cannot be edited right now (user may be dragging a tab)." });
  w.chrome.tabs.update = h.spy(async () => {
    throw new Error("Navigation to chrome://settings is not allowed.");
  });
  assert.equal((await request(ws, "navigate", { url: "chrome://settings" })).error.code, "invalid_argument");
});

test("navigate/createTab: a URL rejected by tabs.update/create is invalid_argument", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.tabs.update = h.spy(async () => {
    throw new Error('Invalid url: "notaurl".');
  });
  const update = await request(ws, "navigate", { url: "notaurl", timeoutMs: 500 });
  assert.equal(update.error.code, "invalid_argument");
  assert.match(update.error.message, /Invalid url/);
  w.chrome.tabs.create = h.spy(async () => {
    throw new Error("Cannot navigate to a file URL without access.");
  });
  const create = await request(ws, "createTab", { url: "file:///etc/hosts", timeoutMs: 500 });
  assert.equal(create.error.code, "invalid_argument");
  assert.equal(w.chrome.webNavigation.onCompleted.hasListeners(), false, "watchers cancelled");
});

// ── exactly one reply ────────────────────────────────────────────────────

test("handler exceptions become one internal reply; unknown methods and hangs are answered", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.tabs.query = h.spy(async () => {
    throw new TypeError("boom");
  });
  const boom = await request(ws, "listTabs");
  assert.deepEqual(boom, { type: "reply", id: boom.id, ok: false, error: { code: "internal", message: "boom" }, tabId: null });

  w.chrome.tabs.get = () => {
    throw new Error("sync boom");
  };
  const sync = await request(ws, "getTabInfo", {}, 1);
  assert.deepEqual(sync.error, { code: "internal", message: "sync boom" });

  const unknown = await request(ws, "fly", {});
  assert.equal(unknown.error.code, "unknown_method");

  w.bg.timing.timeoutGraceMs = 10;
  w.chrome.tabs.get = h.makeApi(w.chrome, async (id) => ({ id, windowId: 1, active: true }));
  w.chrome.tabs.sendMessage = h.spy(() => new Promise(() => {}));
  const hung = await request(ws, "waitForSelector", { selector: "#x", timeoutMs: 20 }, 1);
  assert.equal(hung.error.code, "timeout");
  await sleep(40);
  assert.equal(ws.sent.filter((f) => f.type === "reply" && f.id === hung.id).length, 1);
  for (const id of [boom.id, sync.id, unknown.id]) {
    assert.equal(ws.sent.filter((f) => f.type === "reply" && f.id === id).length, 1);
  }
});

// ── content-routed ───────────────────────────────────────────────────────

test("content-routed: injects the five content files once when the receiving end is missing", async (t) => {
  const w = makeWorker(t, { tabs: [{ id: 1, windowId: 1, active: true, url: "https://example.test/", zoom: 1.25 }] });
  const ws = await welcomed();
  let calls = 0;
  w.chrome.tabs.sendMessage = h.spy(async () => {
    calls += 1;
    if (calls === 1) {
      throw new Error(NO_RECEIVER);
    }
    return { ok: true, result: { pong: true, url: "https://example.test/" } };
  });
  const reply = await request(ws, "ping");
  assert.deepEqual(reply, {
    type: "reply",
    id: reply.id,
    ok: true,
    result: { pong: true, url: "https://example.test/" },
    tabId: 1,
    zoom: 1.25,
  });
  const [tabId, message, options] = plain(w.chrome.tabs.sendMessage.calls[0]);
  assert.equal(tabId, 1);
  assert.deepEqual(message, { __emunium__: 2, id: reply.id, method: "ping", params: {} });
  assert.deepEqual(options, { frameId: 0 });
  assert.deepEqual(plain(w.chrome.scripting.executeScript.calls.map((c) => c[0])), [
    { target: { tabId: 1, frameIds: [0] }, files: CONTENT_FILES },
  ]);

  w.chrome.tabs.sendMessage = h.spy(async () => ({ ok: false, error: { code: "stale_element", message: "gone" } }));
  const stale = await request(ws, "describeElement", { elementId: "abc.1" });
  assert.deepEqual(stale, {
    type: "reply",
    id: stale.id,
    ok: false,
    error: { code: "stale_element", message: "gone" },
    tabId: 1,
    zoom: 1.25,
  });
});

test("content-routed: content_unavailable without a ~10 s retry loop", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.tabs.sendMessage = h.spy(async () => {
    throw new Error(NO_RECEIVER);
  });
  w.chrome.scripting.executeScript = h.spy(async () => {
    throw new Error('Cannot access contents of url "chrome://newtab/".');
  });
  const started = Date.now();
  const blocked = await request(ws, "querySelector", { selector: "a" });
  assert.equal(blocked.error.code, "content_unavailable");
  assert.match(blocked.error.message, /chrome:\/\/newtab/);
  assert.equal(blocked.tabId, 1);
  assert.equal(w.chrome.tabs.sendMessage.calls.length, 1);
  assert.ok(Date.now() - started < 500);

  w.chrome.scripting.executeScript = h.spy(async () => [{ frameId: 0, result: undefined }]);
  w.chrome.tabs.sendMessage.reset();
  const still = await request(ws, "querySelector", { selector: "a" });
  assert.equal(still.error.code, "content_unavailable");
  assert.equal(w.chrome.tabs.sendMessage.calls.length, 2, "one injection, one re-send");
  assert.equal(w.chrome.scripting.executeScript.calls.length, 1);
});

// Runs the MAIN-world evaluator like Chrome: serialized func, page realm with a document.
function mainWorldExecutor(t, { documentId = () => "doc-1-0" } = {}) {
  const doc = h.createDocument();
  const page = h.loadScripts([], h.createWindow(doc));
  t.after(() => page.__harness.dispose());
  const executor = h.spy(async (injection) => {
    if (injection.files) {
      return [{ frameId: 0, documentId: documentId(), result: undefined }];
    }
    const fn = page.__harness.evaluate("(" + injection.func.toString() + ")");
    return [{ frameId: 0, documentId: documentId(), result: fn(...injection.args) }];
  });
  return { doc, executor };
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const listeners = (target, type) => (target.__listeners.get(type) || []).length;

test("waitForSelector with nested custom_js: a fresh channel per wait, evaluator injected with the codes in tree order, then forwarded", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const order = [];
  const { doc, executor } = mainWorldExecutor(t);
  w.chrome.scripting.executeScript = h.spy(async (injection) => {
    order.push("inject");
    return executor(injection);
  });
  const sent = [];
  w.chrome.tabs.sendMessage = h.spy(async (tabId, message) => {
    order.push("send:" + message.method);
    sent.push(plain(message.params));
    return { ok: true, result: { element: null, matched: true, count: 1 } };
  });
  const params = {
    selector: "#a",
    timeoutMs: 1000,
    __mainChannel: "forged-by-caller",
    conditions: [
      { type: "custom_js", code: "return 1" },
      { type: "any_of", conditions: [[{ type: "visible" }, { type: "custom_js", code: "return 2" }], [{ type: "not", condition: [{ type: "custom_js", code: "return 3" }] }]] },
    ],
  };
  const first = await request(ws, "waitForSelector", params);
  assert.equal(first.ok, true);
  const injection = w.chrome.scripting.executeScript.calls[0][0];
  assert.deepEqual(plain(injection.target), { tabId: 1, frameIds: [0] });
  assert.equal(injection.world, "MAIN");
  assert.equal(injection.files, undefined, "no file injection path");
  assert.equal(injection.func, w.bg.mainWorldEvaluator);
  const [channel, codes, ttlMs] = injection.args;
  assert.match(channel, UUID);
  assert.deepEqual(plain(codes), ["return 1", "return 2", "return 3"]);
  assert.equal(ttlMs, 8000, "timeoutMs + 5 s + 2 s margin");
  assert.equal(sent[0].__mainChannel, channel, "the forwarded wait carries the worker's channel");
  for (const type of [`${channel}:0`, `${channel}:2:async`, `${channel}:ready`, `${channel}:close`]) {
    assert.equal(listeners(doc, type), 1, type);
  }

  await request(ws, "waitForSelector", params);
  const second = w.chrome.scripting.executeScript.calls[1][0].args[0];
  assert.match(second, UUID);
  assert.notEqual(second, channel, "every wait has its own channel");
  assert.equal(sent[1].__mainChannel, second);

  await request(ws, "waitForSelector", { selector: "#a", __mainChannel: "forged", conditions: [{ type: "visible" }] });
  assert.equal("__mainChannel" in sent[2], false, "no custom_js: nothing injected, caller channel stripped");
  await request(ws, "querySelector", { selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }] });
  assert.deepEqual(order, ["inject", "send:waitForSelector", "inject", "send:waitForSelector", "send:waitForSelector", "send:querySelector"]);
});

test("custom_js: compile-time csp_blocked / script_error are the reply; the wait is never forwarded", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.tabs.sendMessage = h.spy(async () => ({ ok: true, result: null }));
  w.chrome.scripting.executeScript = pageExecutor(w.chrome, { csp: true });
  w.chrome.userScripts = undefined; // no fallback available
  const blocked = await request(ws, "waitForSelector", { selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }] });
  assert.equal(blocked.error.code, "csp_blocked");
  assert.match(blocked.error.message, /Content Security Policy blocks custom_js/);

  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  const broken = await request(ws, "waitForSelector", {
    selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }, { type: "custom_js", code: "return (" }],
  });
  assert.equal(broken.error.code, "script_error");
  assert.match(broken.error.message, /^custom_js: /);
  assert.equal(w.chrome.tabs.sendMessage.calls.length, 0);

  w.chrome.scripting.executeScript = h.spy(async () => [{ frameId: 0, documentId: "doc-1-0", result: undefined }]);
  const silent = await request(ws, "waitForSelector", { selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }] });
  assert.equal(silent.error.code, "content_unavailable", "no evaluator result: not forwarded");
  assert.equal(w.chrome.scripting.executeScript.calls.length, 2, "retried once before giving up");
  assert.equal(w.chrome.tabs.sendMessage.calls.length, 0);
});

test("custom_js: no frame-0 result (navigation during injection) is retried once on the new document", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const { doc, executor } = mainWorldExecutor(t);
  let first = true;
  w.chrome.scripting.executeScript = h.spy(async (injection) => {
    if (first) {
      first = false;
      return [];
    }
    return executor(injection);
  });
  const sent = [];
  w.chrome.tabs.sendMessage = h.spy(async (tabId, message) => {
    sent.push(plain(message.params));
    return { ok: true, result: null };
  });
  const reply = await request(ws, "waitForSelector", { selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }] });
  assert.equal(reply.ok, true, JSON.stringify(reply));
  assert.equal(w.chrome.scripting.executeScript.calls.length, 2);
  assert.equal(listeners(doc, `${sent[0].__mainChannel}:ready`), 1, "the forwarded channel is live");
});

test("custom_js under CSP: userScripts.execute fallback ships the codes as source with JSON args", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome, { csp: true });
  const doc = h.createDocument();
  const page = h.loadScripts([], h.createWindow(doc));
  t.after(() => page.__harness.dispose());
  // The user script must work where string compilation is forbidden.
  page.__harness.evaluate("Function = function () { throw new EvalError('Code generation from strings disallowed'); };");
  w.chrome.userScripts.execute = h.makeApi(w.chrome, async (injection) => {
    let result;
    try {
      result = page.__harness.evaluate(injection.js[0].code);
    } catch {
      result = undefined; // Chrome: a script that does not parse yields no result
    }
    return [{ frameId: 0, documentId: "doc-1-0", result }];
  });
  const sent = [];
  w.chrome.tabs.sendMessage = h.spy(async (tabId, message) => {
    sent.push(plain(message.params));
    return { ok: true, result: null };
  });
  const codes = ["return el && el.id === 'go';", "return \"quote\\\" and </script>\";"];
  const reply = await request(ws, "waitForSelector", {
    selector: "#a", timeoutMs: 100, conditions: codes.map((code) => ({ type: "custom_js", code })),
  });
  assert.equal(reply.ok, true, JSON.stringify(reply));
  const injection = plain(w.chrome.userScripts.execute.calls[0][0]);
  assert.equal(injection.world, "MAIN");
  assert.deepEqual(injection.target, { tabId: 1, frameIds: [0] });
  const channel = sent[0].__mainChannel;
  assert.match(channel, UUID);
  assert.ok(injection.js[0].code.includes(JSON.stringify(channel)));
  assert.ok(injection.js[0].code.includes(JSON.stringify(codes)), "codes serialized as JSON");
  assert.ok(injection.js[0].code.includes(", 7100, ["), "ttl serialized");
  // The installed evaluator answers through the channel with the shipped functions.
  const button = h.el(doc, "button", { id: "go" });
  doc.body.append(button);
  const answer = new page.Event(`${channel}:0`, { bubbles: true, cancelable: true, composed: true });
  assert.equal(button.dispatchEvent(answer), false, "code 0 is true for #go");
  assert.equal(listeners(doc, `${channel}:1`), 1);

  const broken = await request(ws, "waitForSelector", { selector: "#a", conditions: [{ type: "custom_js", code: "return (" }] });
  assert.equal(broken.error.code, "script_error", "a syntax error breaks the user script: no result");

  w.chrome.userScripts.execute = h.spy(async () => {
    throw new Error("userScripts is not enabled");
  });
  const disabled = await request(ws, "waitForSelector", { selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }] });
  assert.equal(disabled.error.code, "csp_blocked");
  assert.equal(sent.length, 1, "failed injections never forward the wait");
});

test("custom_js: no receiver -> content scripts injected, then a fresh channel and evaluator for the re-send", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const { doc, executor } = mainWorldExecutor(t);
  w.chrome.scripting.executeScript = executor;
  const sent = [];
  w.chrome.tabs.sendMessage = h.spy(async (tabId, message) => {
    sent.push(plain(message.params));
    if (sent.length === 1) throw new Error(NO_RECEIVER);
    return { ok: true, result: null };
  });
  const reply = await request(ws, "waitForSelector", { selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }] });
  assert.equal(reply.ok, true, JSON.stringify(reply));
  const kinds = executor.calls.map((c) => (c[0].files ? "files" : "evaluator"));
  assert.deepEqual(kinds, ["evaluator", "files", "evaluator"]);
  assert.notEqual(sent[1].__mainChannel, sent[0].__mainChannel);
  assert.equal(executor.calls[2][0].args[0], sent[1].__mainChannel);
  assert.equal(listeners(doc, `${sent[1].__mainChannel}:ready`), 1);
});

test("custom_js: ttl is timeoutMs + 5 s + 2 s margin, 17 s by default and clamped to 2^31-1", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const { executor } = mainWorldExecutor(t);
  w.chrome.scripting.executeScript = executor;
  w.chrome.tabs.sendMessage = h.spy(async () => ({ ok: true, result: null }));
  const conditions = [{ type: "custom_js", code: "return 1" }];
  await request(ws, "waitForSelector", { selector: "#a", conditions });
  await request(ws, "waitForSelector", { selector: "#a", conditions, timeoutMs: 3e9 });
  await request(ws, "waitForSelector", { selector: "#a", conditions, timeoutMs: 0 });
  assert.deepEqual(executor.calls.map((c) => c[0].args[2]), [17000, 2147483647, 7000]);
  assert.equal(w.bg.mainEvaluatorTtl(1000), 8000);
});

test("custom_js: a commit during the evaluator injection injects once more into the new document", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  let currentDoc = "doc-A";
  let injectedInto = null;
  w.chrome.webNavigation.getFrame = h.makeApi(w.chrome, async () => ({ frameId: 0, documentId: currentDoc }));
  const { executor } = mainWorldExecutor(t, { documentId: () => injectedInto });
  w.chrome.scripting.executeScript = h.spy(async (injection) => {
    injectedInto = currentDoc;
    if (currentDoc === "doc-A") {
      currentDoc = "doc-B";
      w.chrome.webNavigation.onCommitted.emit({ tabId: 1, frameId: 0, url: "https://b.test/", documentId: "doc-B" });
    }
    return executor(injection);
  });
  w.chrome.tabs.sendMessage = h.spy(async () => ({ ok: true, result: null }));
  const params = { selector: "#a", conditions: [{ type: "custom_js", code: "return 1" }] };
  await request(ws, "waitForSelector", params);
  const calls = w.chrome.scripting.executeScript.calls;
  assert.equal(calls.length, 2, "doc-B got its evaluator");
  assert.equal(calls[1][0].args[0], calls[0][0].args[0], "same channel for the same wait");
  await request(ws, "waitForSelector", params);
  assert.equal(calls.length, 3, "an unchanged document is injected once per wait");
});

test("content-routed: the guard restarts after injection, so a slow injection still ends in wait_timeout", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.bg.timing.timeoutGraceMs = 50;
  let calls = 0;
  w.chrome.tabs.sendMessage = h.spy(async (tabId, message) => {
    calls += 1;
    if (calls === 1) {
      throw new Error(NO_RECEIVER);
    }
    await sleep(message.params.timeoutMs);
    return { ok: false, error: { code: "wait_timeout", message: "no match for #late" } };
  });
  w.chrome.scripting.executeScript = h.spy(async () => {
    await sleep(200); // longer than timeoutMs + grace
    return [{ frameId: 0, documentId: "doc-1-0", result: undefined }];
  });
  const reply = await request(ws, "waitForSelector", { selector: "#late", timeoutMs: 100 }, null, 3000);
  assert.deepEqual(reply.error, { code: "wait_timeout", message: "no match for #late" });
});

// ── executeScript ────────────────────────────────────────────────────────

test("executeScript runs in MAIN world, awaits promises and converts results to JSON", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  const code =
    "return { n: 1, s: 'x', d: new Date(0), u: undefined, f() {}, nan: NaN, big: 10n," +
    " list: [1, undefined, () => 1], set: new Set([1, 2]), map: new Map([['k', 1]])," +
    " err: new Error('bad'), nested: { deep: true } };";
  const reply = await request(ws, "executeScript", { code }, 1);
  assert.equal(reply.ok, true, JSON.stringify(reply));
  assert.deepEqual(reply.result, {
    value: {
      n: 1,
      s: "x",
      d: "1970-01-01T00:00:00.000Z",
      nan: null,
      big: "10",
      list: [1, null, null],
      set: [1, 2],
      map: [["k", 1]],
      err: { name: "Error", message: "bad" },
      nested: { deep: true },
    },
  });
  assert.equal(reply.tabId, 1);
  const injection = w.chrome.scripting.executeScript.calls[0][0];
  assert.equal(injection.world, "MAIN");
  assert.deepEqual(plain(injection.target), { tabId: 1, frameIds: [0] });
  assert.equal(injection.args[0], code);

  const asyncReply = await request(ws, "executeScript", { code: "return new Promise((r) => setTimeout(() => r({ ok: 42 }), 5));" });
  assert.deepEqual(asyncReply.result, { value: { ok: 42 } });
  const cyclic = await request(ws, "executeScript", { code: "const a = { k: 1 }; a.self = a; return a;" });
  assert.deepEqual(cyclic.result, { value: { k: 1, self: "[Circular]" } });
  const nothing = await request(ws, "executeScript", { code: "document_is_not_needed = 1;" });
  assert.deepEqual(nothing.result, { value: null });
});

test("executeScript: the body may await (compiled as an async function)", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  const awaited = await request(ws, "executeScript", {
    code: "const r = await new Promise((done) => setTimeout(() => done({ pong: true }), 5)); return r;",
  });
  assert.equal(awaited.ok, true, JSON.stringify(awaited));
  assert.deepEqual(awaited.result, { value: { pong: true } });
  const rejected = await request(ws, "executeScript", { code: "await Promise.reject(new RangeError('later'));" });
  assert.deepEqual(rejected.error, { code: "script_error", message: "RangeError: later" });
  const hung = await request(ws, "executeScript", { code: "await new Promise(() => {});", timeoutMs: 30 });
  assert.equal(hung.error.code, "timeout");
});

test("executeScript: hostile values become [Unserializable] instead of throwing", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  const code = [
    "const proxy = new Proxy({}, { ownKeys() { throw new Error('ownKeys'); } });",
    "const getter = { ok: 1, get bad() { throw new Error('getter'); } };",
    "const badJSON = { toJSON() { throw new Error('toJSON'); } };",
    "const badLength = new Proxy([], { get(target, key) { if (key === 'length') throw new Error('len'); return target[key]; } });",
    "const badIndex = new Proxy([1, 2], { get(target, key) { if (key === '1') throw new Error('idx'); return target[key]; } });",
    "const revoked = Proxy.revocable({}, {}); revoked.revoke();",
    "return { proxy, getter, badJSON, badLength, badIndex, revoked: revoked.proxy, fine: true };",
  ].join("\n");
  const reply = await request(ws, "executeScript", { code });
  assert.equal(reply.ok, true, JSON.stringify(reply));
  assert.deepEqual(reply.result.value, {
    proxy: "[Unserializable]",
    getter: { ok: 1, bad: "[Unserializable]" },
    badJSON: "[Unserializable]",
    badLength: "[Unserializable]",
    badIndex: [1, "[Unserializable]"],
    revoked: "[Unserializable]",
    fine: true,
  });
  const thrownProxy = await request(ws, "executeScript", {
    code: "throw new Proxy({}, { get() { throw new Error('trap'); } });",
  });
  assert.equal(thrownProxy.error.code, "script_error");
});

test("executeScript: results over the node/size budget are script_error 'result too large'", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  const started = Date.now();
  const sparse = await request(ws, "executeScript", { code: "const a = []; a[50000000] = 1; return a;" });
  assert.equal(sparse.error.code, "script_error");
  assert.match(sparse.error.message, /^result too large/);
  assert.ok(Date.now() - started < 1000, "rejected without iterating the holes");
  const wide = await request(ws, "executeScript", { code: "return Array.from({ length: 600000 }, () => [1, 2]);" }, null, 5000);
  assert.match(wide.error.message, /^result too large/);
  const long = await request(ws, "executeScript", { code: "return ['x'.repeat(20 * 1024 * 1024), 'y'.repeat(13 * 1024 * 1024)];" }, null, 5000);
  assert.match(long.error.message, /^result too large/);
  const ok = await request(ws, "executeScript", { code: "return new Array(1000).fill(7).length;" });
  assert.deepEqual(ok.result, { value: 1000 });
});

test("replies are capped by UTF-8 JSON size (multibyte and control chars), never sent oversized", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const limit = w.bg.timing.maxReplyBytes;
  assert.equal(limit, 60 * 1024 * 1024);
  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  // Both pass the 32 MiB UTF-16 walk budget but exceed 60 MiB as UTF-8 JSON.
  const cjk = await request(ws, "executeScript", { code: "return '\\u4e2d'.repeat(25e6);" }, null, 20000);
  assert.equal(cjk.ok, false);
  assert.equal(cjk.error.code, "script_error");
  assert.match(cjk.error.message, /^result too large \(\d+ bytes, limit 62914560\)/);
  const control = await request(ws, "executeScript", { code: "return '\\u0001'.repeat(2e7);" }, null, 20000);
  assert.equal(control.error.code, "script_error");
  assert.match(control.error.message, /^result too large/);

  w.bg.timing.maxReplyBytes = 4096;
  w.chrome.tabs.sendMessage = h.spy(async () => ({ ok: true, result: { text: "é".repeat(3000) } }));
  const content = await request(ws, "querySelector", { selector: "p" });
  assert.deepEqual(content, {
    type: "reply",
    id: content.id,
    ok: false,
    error: { code: "internal", message: content.error.message },
    tabId: 1,
    zoom: 1,
  });
  assert.match(content.error.message, /^reply too large \(6\d{3} bytes, limit 4096\)/);
  for (const raw of ws.rawSent) {
    assert.ok(Buffer.byteLength(raw, "utf8") <= limit, "no frame above the cap was written");
  }
  const after = await request(ws, "getTabInfo");
  assert.equal(after.ok, true, "the connection keeps serving requests");
});

test("executeScript keeps '__proto__' as an own key", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  const reply = await request(ws, "executeScript", { code: `return JSON.parse('{"__proto__":{"a":1},"b":2}');` });
  assert.equal(reply.ok, true, JSON.stringify(reply));
  assert.deepEqual(Object.keys(reply.result.value), ["__proto__", "b"]);
  assert.deepEqual(reply.result.value, JSON.parse('{"__proto__":{"a":1},"b":2}'));
  assert.deepEqual(Object.getPrototypeOf(reply.result.value), Object.prototype);
});

test("executeScript: user exceptions are script_error, hangs are timeout", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome);
  const thrown = await request(ws, "executeScript", { code: "throw new TypeError('nope');" });
  assert.deepEqual(thrown.error, { code: "script_error", message: "TypeError: nope" });
  const syntax = await request(ws, "executeScript", { code: "return (" });
  assert.equal(syntax.error.code, "script_error");
  assert.match(syntax.error.message, /^SyntaxError/);
  const hung = await request(ws, "executeScript", { code: "return new Promise(() => {});", timeoutMs: 30 });
  assert.equal(hung.error.code, "timeout");
  const missing = await request(ws, "executeScript", {});
  assert.equal(missing.error.code, "invalid_argument");
});

test("executeScript CSP path: userScripts.execute fallback, else csp_blocked", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  w.chrome.scripting.executeScript = pageExecutor(w.chrome, { csp: true });
  w.chrome.userScripts.execute = h.makeApi(w.chrome, async (injection) => {
    // The fallback ships source text; it must run where string compilation is forbidden.
    const page = vm.createContext({}, { codeGeneration: { strings: false, wasm: false } });
    const result = await vm.runInContext(injection.js[0].code, page);
    return [{ frameId: 0, result }];
  });
  const viaUserScripts = await request(ws, "executeScript", { code: "return [6 * 7, typeof window];" }, 1);
  assert.equal(viaUserScripts.ok, true, JSON.stringify(viaUserScripts));
  assert.deepEqual(viaUserScripts.result, { value: [42, "undefined"] });
  const injection = plain(w.chrome.userScripts.execute.calls[0][0]);
  assert.equal(injection.world, "MAIN");
  assert.deepEqual(injection.target, { tabId: 1, frameIds: [0] });

  const failing = await request(ws, "executeScript", { code: "throw new Error('inner');" });
  assert.deepEqual(failing.error, { code: "script_error", message: "Error: inner" });
  const awaited = await request(ws, "executeScript", { code: "const v = await Promise.resolve(6); return v * 7;" });
  assert.equal(awaited.ok, true, JSON.stringify(awaited));
  assert.deepEqual(awaited.result, { value: 42 });

  w.chrome.userScripts.execute = h.spy(async () => {
    throw new Error("userScripts is not enabled");
  });
  const disabled = await request(ws, "executeScript", { code: "return 1;" });
  assert.equal(disabled.error.code, "csp_blocked");

  w.chrome.userScripts = undefined;
  const blocked = await request(ws, "executeScript", { code: "return 1;" });
  assert.equal(blocked.error.code, "csp_blocked");
  assert.match(blocked.error.message, /Content Security Policy/);
});

// ── network delegation ───────────────────────────────────────────────────

test("network methods delegate with the S3 signatures", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const entry = { url: "https://api.test/x", statusCode: 200, method: "GET", type: "xmlhttprequest", timeStamp: 5, tabId: 1, requestId: "7" };
  w.bg.waitForResponse = h.spy(async () => entry);
  const found = await request(ws, "waitForResponse", { pattern: "*api*", timeoutMs: 500, since: 123, anyTab: false }, 1);
  assert.deepEqual(found.result, entry);
  assert.equal(found.tabId, 1);
  assert.deepEqual(plain(w.bg.waitForResponse.calls[0]), [
    { pattern: "*api*", timeoutMs: 500, tabId: 1, since: 123, anyTab: false },
  ]);

  w.bg.waitForResponse = h.spy(async () => null);
  const none = await request(ws, "waitForResponse", { pattern: "*", timeoutMs: 10, since: null, anyTab: true });
  assert.equal(none.ok, true);
  assert.equal(none.result, null);
  assert.deepEqual(plain(w.bg.waitForResponse.calls[0]), [
    { pattern: "*", timeoutMs: 10, tabId: null, since: null, anyTab: true },
  ]);

  w.bg.getRecentResponses = h.spy(async () => [entry]);
  const recent = await request(ws, "getRecentResponses", { since: 4 });
  assert.deepEqual(recent.result, [entry]);
  assert.deepEqual(plain(w.bg.getRecentResponses.calls[0]), [{ since: 4, tabId: 1, anyTab: false }]);

  const invalid = new Error("bad glob");
  invalid.code = "invalid_argument";
  w.bg.waitForResponse = h.spy(async () => {
    throw invalid;
  });
  assert.equal((await request(ws, "waitForResponse", { pattern: "[" })).error.code, "invalid_argument");
  assert.equal((await request(ws, "waitForResponse", {})).error.code, "invalid_argument");
});

// ── runtime messages ─────────────────────────────────────────────────────

test("tabReady only from top-frame v2 readiness; the content-side exec path is gone", async (t) => {
  const w = makeWorker(t);
  const ws = await welcomed();
  const onMessage = w.chrome.runtime.onMessage;
  await h.dispatchMessage(onMessage, { __emunium_ready__: 2, url: "https://a.test/" }, { tab: { id: 1 }, frameId: 2 });
  await h.dispatchMessage(onMessage, { __emunium_ready__: true, url: "https://a.test/" }, { tab: { id: 1 }, frameId: 0 });
  assert.equal(events(ws, "tabReady").length, 0);
  await h.dispatchMessage(onMessage, { __emunium_ready__: 2, url: "https://a.test/" }, { tab: { id: 1 }, frameId: 0 });
  assert.deepEqual(events(ws, "tabReady"), [{ type: "event", event: "tabReady", tabId: 1, url: "https://a.test/" }]);

  const response = await h.dispatchMessage(
    onMessage,
    { __emunium_exec__: true, code: "return 1" },
    { tab: { id: 1 }, frameId: 0 }
  );
  assert.equal(response, undefined);
  await h.tick();
  assert.equal(w.chrome.scripting.executeScript.calls.length, 0);
  assert.equal(typeof w.bg.handleExecMessage, "undefined");
});
