"use strict";
// tests/js/network.test.js - background_network.js: tab-scoped buffer and waiters
// (docs/architecture/bridge-protocol.md section 6).

const test = require("node:test");
const assert = require("node:assert/strict");
const h = require("./harness");

const T0 = 1700000000000;

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeNetwork(t, { chrome } = {}) {
  const fakeChrome = chrome === undefined ? h.createFakeChrome() : chrome;
  const globals = { chrome: fakeChrome, EmuniumBackground: {} };
  const ctx = h.loadScripts(["background_network.js"], globals);
  // Fake wall clock inside the context only; timers stay real and short.
  ctx.__fakeNow = T0;
  ctx.__harness.evaluate("Date.now = () => globalThis.__fakeNow;");
  t.after(() => ctx.__harness.dispose());
  const bg = ctx.EmuniumBackground;
  let seq = 0;
  const complete = (details) => {
    seq += 1;
    fakeChrome.webRequest.onCompleted.emit({
      requestId: String(seq),
      method: "GET",
      type: "xmlhttprequest",
      statusCode: 200,
      timeStamp: ctx.__fakeNow,
      tabId: 1,
      ...details,
    });
  };
  const fail = (details) => {
    seq += 1;
    fakeChrome.webRequest.onErrorOccurred.emit({
      requestId: String(seq),
      method: "GET",
      type: "xmlhttprequest",
      timeStamp: ctx.__fakeNow,
      tabId: 1,
      error: "net::ERR_CONNECTION_REFUSED",
      ...details,
    });
  };
  return { chrome: fakeChrome, ctx, bg, complete, fail };
}

test("loads without chrome.webRequest and without chrome", async (t) => {
  const bare = h.createFakeChrome();
  delete bare.webRequest;
  const a = makeNetwork(t, { chrome: bare });
  assert.equal(typeof a.bg.waitForResponse, "function");
  assert.deepEqual(plain(a.bg.getRecentResponses({ since: null, tabId: 1, anyTab: false })), []);
  assert.equal(await a.bg.waitForResponse({ pattern: "*", timeoutMs: 0, tabId: 1, since: null, anyTab: false }), null);

  const ctx = h.loadScripts(["background_network.js"], { chrome: undefined, EmuniumBackground: {} });
  t.after(() => ctx.__harness.dispose());
  assert.equal(typeof ctx.EmuniumBackground.getRecentResponses, "function");
});

test("records completed and failed requests with the protocol entry shape", (t) => {
  const n = makeNetwork(t);
  assert.equal(n.chrome.webRequest.onCompleted.listeners.length, 1);
  assert.equal(n.chrome.webRequest.onErrorOccurred.listeners.length, 1);
  n.complete({ url: "https://a.test/ok", statusCode: 204, method: "POST", requestId: "41" });
  n.fail({ url: "https://a.test/down", requestId: "42" });
  assert.deepEqual(plain(n.bg.getRecentResponses({ since: null, tabId: 1, anyTab: false })), [
    { url: "https://a.test/ok", statusCode: 204, method: "POST", type: "xmlhttprequest", timeStamp: T0, tabId: 1, requestId: "41" },
    {
      url: "https://a.test/down",
      statusCode: 0,
      method: "GET",
      type: "xmlhttprequest",
      timeStamp: T0,
      tabId: 1,
      requestId: "42",
      error: "net::ERR_CONNECTION_REFUSED",
    },
  ]);
});

test("tab scoping: tabId must equal, null tabId means any tab >= 0, anyTab includes tabId < 0", (t) => {
  const n = makeNetwork(t);
  n.complete({ url: "https://a.test/one", tabId: 1 });
  n.complete({ url: "https://a.test/two", tabId: 2 });
  n.complete({ url: "https://a.test/bg", tabId: -1 });
  const urls = (filter) => plain(n.bg.getRecentResponses(filter).map((e) => e.url));
  assert.deepEqual(urls({ since: null, tabId: 1, anyTab: false }), ["https://a.test/one"]);
  assert.deepEqual(urls({ since: null, tabId: 2, anyTab: false }), ["https://a.test/two"]);
  assert.deepEqual(urls({ since: null, tabId: null, anyTab: false }), ["https://a.test/one", "https://a.test/two"]);
  assert.deepEqual(urls({ since: null, tabId: 1, anyTab: true }), [
    "https://a.test/one",
    "https://a.test/two",
    "https://a.test/bg",
  ]);
});

test("since filters by timeStamp >= since; null since means no filter", (t) => {
  const n = makeNetwork(t);
  n.complete({ url: "https://a.test/early", timeStamp: T0 - 10 });
  n.complete({ url: "https://a.test/edge", timeStamp: T0 });
  n.complete({ url: "https://a.test/late", timeStamp: T0 + 5 });
  const urls = (since) => plain(n.bg.getRecentResponses({ since, tabId: 1, anyTab: false }).map((e) => e.url));
  assert.deepEqual(urls(T0), ["https://a.test/edge", "https://a.test/late"]);
  assert.deepEqual(urls(null), ["https://a.test/early", "https://a.test/edge", "https://a.test/late"]);
});

test("glob: only * is a wildcard; ? . + ( [ are literal", async (t) => {
  const n = makeNetwork(t);
  n.complete({ url: "https://a.test/xYid=1" });
  n.complete({ url: "https://a.test/api.v1/(list)+[x]" });
  const wait = (pattern) => n.bg.waitForResponse({ pattern, timeoutMs: 0, tabId: 1, since: null, anyTab: false });
  assert.equal(await wait("https://a.test/x?id=1"), null);
  n.complete({ url: "https://a.test/x?id=1" });
  assert.equal((await wait("https://a.test/x?id=1")).url, "https://a.test/x?id=1");
  assert.equal(await wait("https://a.test/apiXv1/*"), null);
  assert.equal((await wait("https://a.test/api.v1/(list)+[x]")).url, "https://a.test/api.v1/(list)+[x]");
  assert.equal((await wait("*a.test/api*")).url, "https://a.test/api.v1/(list)+[x]");
  assert.equal(await wait("a.test/*"), null, "glob is anchored at both ends");
});

test("invalid pattern rejects with code invalid_argument", async (t) => {
  const n = makeNetwork(t);
  for (const pattern of ["", null, 7]) {
    await assert.rejects(
      n.bg.waitForResponse({ pattern, timeoutMs: 10, tabId: 1, since: null, anyTab: false }),
      (error) => error.code === "invalid_argument"
    );
  }
  assert.equal(n.bg.networkState.pendingWaiters.length, 0);
});

test("buffer keeps at most 500 entries and none older than 120 s", (t) => {
  const n = makeNetwork(t);
  for (let i = 0; i < 600; i += 1) {
    n.complete({ url: `https://a.test/r${i}` });
  }
  let all = n.bg.getRecentResponses({ since: null, tabId: null, anyTab: true });
  assert.equal(all.length, 500);
  assert.equal(all[0].url, "https://a.test/r100");
  assert.equal(all[499].url, "https://a.test/r599");

  n.ctx.__fakeNow = T0 + 60000;
  n.complete({ url: "https://a.test/fresh" });
  n.ctx.__fakeNow = T0 + 120001;
  all = n.bg.getRecentResponses({ since: null, tabId: null, anyTab: true });
  assert.deepEqual(plain(all.map((e) => e.url)), ["https://a.test/fresh"]);
});

test("buffered match first, scoped by tab and since", async (t) => {
  const n = makeNetwork(t);
  n.complete({ url: "https://a.test/api", tabId: 2, requestId: "other-tab" });
  n.complete({ url: "https://a.test/api", tabId: 1, timeStamp: T0 - 1, requestId: "too-old" });
  n.complete({ url: "https://a.test/api", tabId: 1, requestId: "hit" });
  const entry = await n.bg.waitForResponse({ pattern: "*api", timeoutMs: 0, tabId: 1, since: T0, anyTab: false });
  assert.equal(entry.requestId, "hit");
  const any = await n.bg.waitForResponse({ pattern: "*api", timeoutMs: 0, tabId: 1, since: null, anyTab: true });
  assert.equal(any.requestId, "other-tab");
});

test("waiter resolves on a later matching event and ignores other tabs and error filters", async (t) => {
  const n = makeNetwork(t);
  const pending = n.bg.waitForResponse({ pattern: "https://a.test/*", timeoutMs: 1000, tabId: 1, since: T0, anyTab: false });
  assert.equal(n.bg.networkState.pendingWaiters.length, 1);
  n.complete({ url: "https://a.test/x", tabId: 2 });
  n.complete({ url: "https://a.test/x", tabId: -1 });
  n.complete({ url: "https://a.test/x", tabId: 1, timeStamp: T0 - 1 });
  n.complete({ url: "https://b.test/x", tabId: 1 });
  assert.equal(n.bg.networkState.pendingWaiters.length, 1);
  n.fail({ url: "https://a.test/x", tabId: 1, requestId: "failed" });
  const entry = await pending;
  assert.equal(entry.requestId, "failed");
  assert.equal(entry.statusCode, 0);
  assert.equal(entry.error, "net::ERR_CONNECTION_REFUSED");
  assert.equal(n.bg.networkState.pendingWaiters.length, 0);
});

test("anyTab waiter accepts background requests (tabId < 0)", async (t) => {
  const n = makeNetwork(t);
  const pending = n.bg.waitForResponse({ pattern: "*", timeoutMs: 1000, tabId: null, since: null, anyTab: true });
  n.complete({ url: "https://a.test/sw", tabId: -1 });
  assert.equal((await pending).tabId, -1);
});

test("waiter timeout resolves null and removes the waiter", async (t) => {
  const n = makeNetwork(t);
  const started = process.hrtime.bigint();
  const result = await n.bg.waitForResponse({ pattern: "*nothing*", timeoutMs: 20, tabId: 1, since: null, anyTab: false });
  const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
  assert.equal(result, null);
  assert.ok(elapsedMs >= 15, `timed out too early (${elapsedMs} ms)`);
  assert.equal(n.bg.networkState.pendingWaiters.length, 0);
  n.complete({ url: "https://a.test/nothing-here" });
  assert.equal(n.bg.networkState.pendingWaiters.length, 0);
});
