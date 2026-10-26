"use strict";
// tests/js/attach.test.js - attach mode of the service worker (docs/architecture/attach-mode.md sections 2-4).
// Run: node --test tests/js/attach.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const h = require("./harness");

const SESSION = { protocol: 2, port: 4321, token: "tok-abc" };
const ATTACH = { protocol: 2, mode: "attach", hubUrl: "ws://127.0.0.1:47800", enrollment: "enr-secret" };
const NONCE = "Ab3_-xYz0123456789_nonce";
const CLAIM_URL = `http://127.0.0.1:47800/pair#n=${NONCE}`;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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

function makeWorker(t, { files, storage, before } = {}) {
  h.FakeWebSocket.reset();
  const chrome = h.createFakeChrome({ files: files === undefined ? { "attach.json": ATTACH } : files });
  // The harness models chrome.storage.local; seed it per test (survives "restarts" by reuse).
  if (!chrome.storage || !chrome.storage.local) {
    throw new Error("harness lacks chrome.storage.local");
  }
  if (storage) {
    chrome.__state.storage.local = storage;
  }
  if (before) {
    before(chrome);
  }
  const globals = { chrome, WebSocket: h.FakeWebSocket, navigator: { userAgent: "test-agent/1.0" } };
  const ctx = h.loadScripts(["background.js"], globals);
  const bg = ctx.EmuniumBackground;
  bg.waitForResponse = h.spy(async () => null);
  bg.getRecentResponses = h.spy(async () => []);
  t.after(() => {
    // An in-flight loadConfig may resolve after dispose and schedule a retry: refuse new timers.
    ctx.setTimeout = () => 0;
    ctx.setInterval = () => 0;
    ctx.__harness.dispose();
  });
  return { chrome, ctx, bg };
}

const claims = (ws) => ws.sent.filter((f) => f && f.type === "event" && f.event === "claim");
const blanked = (chrome) => chrome.tabs.update.calls.filter(([, props]) => props && props.url === "about:blank");
const commit = (chrome, details) => chrome.webNavigation.onCommitted.emit(Object.assign({ frameId: 0, tabId: 1 }, details));

// ── config selection ─────────────────────────────────────────────────────

test("loadConfig: session.json wins, else attach.json, else null (retried)", async (t) => {
  const both = makeWorker(t, { files: { "session.json": SESSION, "attach.json": ATTACH } });
  assert.deepEqual(plain(await both.bg.loadConfig()), { url: "ws://127.0.0.1:4321", token: "tok-abc", protocol: 2 });
  assert.equal((await until(() => h.FakeWebSocket.last())).url, "ws://127.0.0.1:4321");
  assert.equal(both.chrome.storage.local.get.calls.length, 0, "launch mode never touches storage");

  const w = makeWorker(t, { files: {} });
  w.bg.timing.configRetryMs = 20;
  assert.equal(await w.bg.loadConfig(), null);
  const invalid = [
    Object.assign({}, ATTACH, { protocol: 1 }),
    Object.assign({}, ATTACH, { mode: "launch" }),
    Object.assign({}, ATTACH, { enrollment: "" }),
    Object.assign({}, ATTACH, { hubUrl: "ws://localhost:47800" }),
    Object.assign({}, ATTACH, { hubUrl: "wss://127.0.0.1:47800" }),
    Object.assign({}, ATTACH, { hubUrl: "ws://127.0.0.1:47800/x" }),
    Object.assign({}, ATTACH, { hubUrl: "ws://u@127.0.0.1:47800" }),
    Object.assign({}, ATTACH, { hubUrl: 47800 }),
  ];
  for (const data of invalid) {
    w.chrome.__state.files["attach.json"] = data;
    assert.equal(await w.bg.loadConfig(), null, JSON.stringify(data));
  }
  w.chrome.__state.files["session.json"] = { protocol: 2, port: 0, token: "x" };
  w.chrome.__state.files["attach.json"] = ATTACH;
  assert.equal(await w.bg.loadConfig(), null, "an invalid session.json still wins (launch mode unchanged)");
  assert.equal(h.FakeWebSocket.instances.length, 0);

  delete w.chrome.__state.files["session.json"];
  const config = plain(await w.bg.loadConfig());
  assert.match(config.installId, UUID_RE);
  assert.deepEqual(Object.assign({}, config, { installId: "x" }), {
    mode: "attach",
    url: "ws://127.0.0.1:47800",
    hubPort: 47800,
    enrollment: "enr-secret",
    protocol: 2,
    installId: "x",
    token: null,
  });
  assert.equal((await until(() => h.FakeWebSocket.last())).url, "ws://127.0.0.1:47800", "the retry loop picks it up");
});

// ── identity and handshake ───────────────────────────────────────────────

test("installId is created once, kept in chrome.storage.local, and sent in the attach hello", async (t) => {
  const storage = {};
  const w = makeWorker(t, { storage });
  const ws = await until(() => h.FakeWebSocket.last());
  ws.open();
  const [hello] = ws.sent;
  assert.match(hello.installId, UUID_RE);
  assert.deepEqual(hello, {
    type: "hello",
    protocol: 2,
    mode: "attach",
    installId: hello.installId,
    enrollment: "enr-secret",
    token: null,
    extensionVersion: "3.1.0",
    userAgent: "test-agent/1.0",
  });
  assert.equal(w.chrome.__state.storage.local.installId, hello.installId);

  const [a, b] = await Promise.all([w.bg.loadConfig(), w.bg.loadConfig()]);
  assert.equal(a.installId, hello.installId);
  assert.equal(b.installId, hello.installId);

  // A restarted worker in the same profile reuses the stored id.
  const again = makeWorker(t, { storage: w.chrome.__state.storage.local });
  const ws2 = await until(() => h.FakeWebSocket.last());
  ws2.open();
  assert.equal(ws2.sent[0].installId, hello.installId);
  assert.equal(again.chrome.storage.local.set.calls.length, 0, "no new id minted");

  // Concurrent first runs mint exactly one id.
  const fresh = makeWorker(t, { files: {} });
  fresh.chrome.__state.files["attach.json"] = ATTACH;
  const ids = (await Promise.all([fresh.bg.loadConfig(), fresh.bg.loadConfig(), fresh.bg.loadIdentity()])).map(
    (c) => c.installId
  );
  assert.equal(new Set(ids).size, 1);
  assert.equal(fresh.chrome.__state.storage.local.installId, ids[0]);
});

test("a welcome carrying a token stores it; the next hello sends it; a token-less welcome keeps it", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.backoffMinMs = 5;
  const ws = await until(() => h.FakeWebSocket.last());
  ws.open();
  ws.serverSend({ type: "welcome", protocol: 2, installId: ws.sent[0].installId, token: "tok-new" });
  assert.equal(w.bg.isConnected(), true);
  await until(() => w.chrome.__state.storage.local.token === "tok-new");

  ws.serverClose(1006);
  const ws2 = await until(() => h.FakeWebSocket.instances[1]);
  ws2.open();
  assert.equal(ws2.sent[0].token, "tok-new");
  assert.equal(ws2.sent[0].installId, ws.sent[0].installId);
  ws2.serverSend({ type: "welcome", protocol: 2, installId: ws.sent[0].installId });
  await h.tick();
  assert.equal(w.chrome.__state.storage.local.token, "tok-new");

  const launch = makeWorker(t, { files: { "session.json": SESSION } });
  const ws3 = await until(() => h.FakeWebSocket.last());
  ws3.open();
  ws3.serverSend({ type: "welcome", protocol: 2, token: "ignored" });
  await h.tick();
  assert.equal(launch.chrome.storage.local.set.calls.length, 0, "launch mode stores nothing");
});

test("reconnect backoff: attach doubles 1 s -> 30 s, launch keeps 1 s -> 5 s", async (t) => {
  const w = makeWorker(t);
  const ws = await until(() => h.FakeWebSocket.last());
  assert.equal(w.bg.state.config.mode, "attach");
  w.bg.resetBackoff();
  assert.deepEqual(
    Array.from({ length: 8 }, () => w.bg.takeBackoff()),
    [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]
  );
  w.bg.state.backoffMs = 16000;
  ws.fail();
  assert.equal(w.bg.state.backoffMs, 30000, "socket close uses the attach cap");
  w.bg.clearReconnect();

  const launch = makeWorker(t, { files: { "session.json": SESSION } });
  const lws = await until(() => h.FakeWebSocket.last());
  launch.bg.resetBackoff();
  assert.deepEqual(Array.from({ length: 5 }, () => launch.bg.takeBackoff()), [1000, 2000, 4000, 5000, 5000]);
  launch.bg.state.backoffMs = 16000;
  lws.fail();
  assert.equal(launch.bg.state.backoffMs, 5000);
  launch.bg.clearReconnect();
});

// ── claim ────────────────────────────────────────────────────────────────

test("parseClaimUrl accepts only http://127.0.0.1:<hubPort>/pair#n=<nonce>", (t) => {
  const { bg } = makeWorker(t, { files: {} });
  const n16 = "a".repeat(16);
  const n128 = "Z_-9".repeat(32);
  assert.equal(bg.parseClaimUrl(CLAIM_URL, 47800), NONCE);
  assert.equal(bg.parseClaimUrl(`http://127.0.0.1:47800/pair#n=${n16}`, 47800), n16);
  assert.equal(bg.parseClaimUrl(`http://127.0.0.1:47800/pair#n=${n128}`, 47800), n128);
  assert.equal(bg.parseClaimUrl(`http://127.0.0.1/pair#n=${n16}`, 80), n16, "canonical URL drops :80");
  const rejected = [
    `http://127.0.0.1:47801/pair#n=${NONCE}`,
    `http://127.0.0.1:047800/pair#n=${NONCE}`,
    `https://127.0.0.1:47800/pair#n=${NONCE}`,
    `http://localhost:47800/pair#n=${NONCE}`,
    `http://127.0.0.2:47800/pair#n=${NONCE}`,
    `http://u@127.0.0.1:47800/pair#n=${NONCE}`,
    `http://127.0.0.1:47800.evil.test/pair#n=${NONCE}`,
    `http://127.0.0.1:47800/pair?x=1#n=${NONCE}`,
    `http://127.0.0.1:47800/pair/#n=${NONCE}`,
    `http://127.0.0.1:47800/pairs#n=${NONCE}`,
    `http://127.0.0.1:47800/x/pair#n=${NONCE}`,
    `http://127.0.0.1:47800/pair#m=${NONCE}`,
    `http://127.0.0.1:47800/pair#n=${NONCE}&x=1`,
    `http://127.0.0.1:47800/pair#n=${NONCE}\n`,
    `http://127.0.0.1:47800/pair#n=${"a".repeat(15)}`,
    `http://127.0.0.1:47800/pair#n=${"a".repeat(129)}`,
    `http://127.0.0.1:47800/pair#n=${"a".repeat(15)}.`,
    `http://127.0.0.1:47800/pair#n=${"a".repeat(15)}%41`,
    `http://127.0.0.1:47800/pair#n=`,
    `http://127.0.0.1:47800/pair`,
    `about:blank`,
  ];
  for (const url of rejected) {
    assert.equal(bg.parseClaimUrl(url, 47800), null, url);
  }
  assert.equal(bg.parseClaimUrl(CLAIM_URL, null), null);
  assert.equal(bg.parseClaimUrl(null, 47800), null);
  assert.equal(bg.hubPortOf("ws://127.0.0.1:47800/"), 47800);
  assert.equal(bg.hubPortOf("ws://127.0.0.1"), 80);
});

test("claim: a commit before the welcome is queued, sent only after the welcome, tab blanked", async (t) => {
  const w = makeWorker(t);
  // Fired before loadConfig resolved: the worker reads the files itself.
  commit(w.chrome, { url: CLAIM_URL });
  await until(() => blanked(w.chrome).length === 1);
  assert.deepEqual(plain(blanked(w.chrome)[0]), [1, { url: "about:blank" }]);

  const ws = await until(() => h.FakeWebSocket.last());
  ws.open();
  await sleep(20);
  assert.deepEqual(
    ws.sent.map((f) => f.type),
    ["hello"],
    "nothing but hello on a socket that is not welcomed"
  );
  ws.serverSend({ type: "welcome", protocol: 2, token: "tok" });
  assert.deepEqual(claims(ws), [{ type: "event", event: "claim", nonce: NONCE }]);
  assert.equal(w.bg.state.pendingClaims.length, 0);

  // Same nonce again (reload before about:blank landed): ignored.
  commit(w.chrome, { url: CLAIM_URL });
  await sleep(20);
  assert.equal(claims(ws).length, 1);
  assert.equal(blanked(w.chrome).length, 1);
});

test("claim: sent at once when welcomed; queued across a reconnect; never on the unwelcomed socket", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.backoffMinMs = 5;
  const ws = await until(() => h.FakeWebSocket.last());
  ws.open();
  ws.serverSend({ type: "welcome", protocol: 2 });
  commit(w.chrome, { url: CLAIM_URL });
  await until(() => claims(ws).length === 1);

  ws.serverClose(1006);
  const second = `http://127.0.0.1:47800/pair#n=${"q".repeat(40)}`;
  commit(w.chrome, { url: second });
  await until(() => blanked(w.chrome).length === 2);
  const ws2 = await until(() => h.FakeWebSocket.instances[1]);
  ws2.open();
  await sleep(10);
  assert.equal(claims(ws2).length, 0);
  assert.equal(ws.droppedSent.length, 0);
  ws2.serverSend({ type: "welcome", protocol: 2 });
  assert.deepEqual(claims(ws2), [{ type: "event", event: "claim", nonce: "q".repeat(40) }]);
  assert.equal(claims(ws).length, 1);
});

test("claim: dropped after claimTtlMs; sub-frames, other ports and launch mode are ignored", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.claimTtlMs = 30;
  const ws = await until(() => h.FakeWebSocket.last());
  commit(w.chrome, { url: CLAIM_URL });
  await until(() => w.bg.state.pendingClaims.length === 1);
  await sleep(60);
  ws.open();
  ws.serverSend({ type: "welcome", protocol: 2 });
  assert.equal(claims(ws).length, 0, "expired claim is not sent");
  assert.equal(w.bg.state.pendingClaims.length, 0);

  commit(w.chrome, { frameId: 3, url: `http://127.0.0.1:47800/pair#n=${"f".repeat(20)}` });
  commit(w.chrome, { url: `http://127.0.0.1:47801/pair#n=${"p".repeat(20)}` });
  await sleep(20);
  assert.equal(claims(ws).length, 0);
  assert.equal(blanked(w.chrome).length, 1, "only the first (expired) claim blanked its tab");

  const launch = makeWorker(t, { files: { "session.json": SESSION, "attach.json": ATTACH } });
  const lws = await until(() => h.FakeWebSocket.last());
  lws.open();
  lws.serverSend({ type: "welcome", protocol: 2 });
  commit(launch.chrome, { url: CLAIM_URL });
  await sleep(20);
  assert.equal(claims(lws).length, 0);
  assert.equal(blanked(launch.chrome).length, 0);
});

test("manifest: storage permission and minimum_chrome_version 116", () => {
  const manifest = require("node:fs").readFileSync(require("node:path").join(h.EXTENSION_DIR, "manifest.json"), "utf8");
  const parsed = JSON.parse(manifest);
  assert.ok(parsed.permissions.includes("storage"));
  assert.equal(parsed.minimum_chrome_version, "116");
});
