"use strict";
// tests/js/attach.test.js - attach mode of the service worker (docs/architecture/attach-mode.md sections 2-4).
// Run: node --test tests/js/attach.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const nodeCrypto = require("node:crypto");
const h = require("./harness");

const SESSION = { protocol: 2, port: 4321, token: "tok-abc" };
const ATTACH = { protocol: 2, mode: "attach", hubUrl: "ws://127.0.0.1:47800", enrollment: "enr-secret" };
const NONCE = "Ab3_-xYz0123456789_nonce";
const CLAIM_URL = `http://127.0.0.1:47800/pair#n=${NONCE}`;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NONCE_RE = /^[A-Za-z0-9_-]{43}$/;
const INSTALL_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const plain = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

// Expected values are computed independently with Node's crypto (HMAC-SHA-256, UTF-8, base64url unpadded).
const mac = (key, message) =>
  nodeCrypto.createHmac("sha256", Buffer.from(key, "utf8")).update(Buffer.from(message, "utf8")).digest("base64url");
const newNonce = () => nodeCrypto.randomBytes(32).toString("base64url");
const serverProofFor = (hello, serverNonce, enrollment = ATTACH.enrollment) =>
  mac(enrollment, `emunium-server|${serverNonce}|${hello.clientNonce}|${hello.installId}`);

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
  const warnings = [];
  const quietConsole = Object.assign(Object.create(console), { warn: (...args) => warnings.push(args.join(" ")) });
  // The harness crypto lacks subtle; the worker uses Web Crypto as in a MV3 service worker.
  const crypto = {
    randomUUID: () => nodeCrypto.randomUUID(),
    getRandomValues: (array) => nodeCrypto.webcrypto.getRandomValues(array),
    subtle: nodeCrypto.webcrypto.subtle,
  };
  const globals = { chrome, crypto, console: quietConsole, WebSocket: h.FakeWebSocket, navigator: { userAgent: "test-agent/1.0" } };
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
  return { chrome, ctx, bg, warnings };
}

const claims = (ws) => ws.sent.filter((f) => f && f.type === "event" && f.event === "claim");
const blanked = (chrome) => chrome.tabs.update.calls.filter(([, props]) => props && props.url === "about:blank");
const commit = (chrome, details) => chrome.webNavigation.onCommitted.emit(Object.assign({ frameId: 0, tabId: 1 }, details));
const closeCodes = (ws) => ws.closeCalls.map((c) => c.code);

// Genuine hub, step 1: open, send the challenge, wait for the hello.
async function challenge(ws) {
  ws.open();
  const serverNonce = newNonce();
  ws.serverSend({ type: "challenge", protocol: 2, serverNonce });
  const hello = await until(() => ws.sent.find((f) => f && f.type === "hello"));
  return { hello, serverNonce };
}

// Genuine hub, step 2: a welcome with a valid serverProof; waits until the worker is connected.
async function welcome(w, ws, { hello, serverNonce }, extra = {}) {
  ws.serverSend(
    Object.assign({ type: "welcome", protocol: 2, installId: hello.installId, serverProof: serverProofFor(hello, serverNonce) }, extra)
  );
  await until(() => w.bg.isConnected() && w.bg.state.ws === ws);
}

async function handshake(w, ws, extra) {
  const hs = await challenge(ws);
  await welcome(w, ws, hs, extra);
  return hs;
}

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
    Object.assign({}, ATTACH, { hubUrl: "ws://127.0.0.1" }),
    Object.assign({}, ATTACH, { hubUrl: "ws://127.0.0.1/" }),
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

test("hubUrl needs an explicit port: a port-less URL is invalid config (logged, retried, never port 80)", async (t) => {
  const w = makeWorker(t, { files: { "attach.json": Object.assign({}, ATTACH, { hubUrl: "ws://127.0.0.1" }) } });
  w.bg.timing.configRetryMs = 10;
  const { bg } = w;
  assert.equal(bg.hubPortOf("ws://127.0.0.1:47800"), 47800);
  assert.equal(bg.hubPortOf("ws://127.0.0.1:47800/"), 47800);
  assert.equal(bg.hubPortOf("ws://127.0.0.1:80"), 80, "an explicit :80 is still explicit");
  assert.equal(bg.hubPortOf("ws://127.0.0.1:65535"), 65535);
  for (const url of [
    "ws://127.0.0.1",
    "ws://127.0.0.1/",
    "ws://127.0.0.1:",
    "ws://127.0.0.1:0",
    "ws://127.0.0.1:047800",
    "ws://127.0.0.1:65536",
    "ws://127.0.0.1:47800?x",
    "ws://127.0.0.1:47800#x",
    " ws://127.0.0.1:47800",
    null,
  ]) {
    assert.equal(bg.hubPortOf(url), null, String(url));
  }
  await sleep(60);
  assert.equal(h.FakeWebSocket.instances.length, 0, "no socket to port 80");
  assert.equal(w.warnings.length, 1, "logged once across retries");
  assert.match(w.warnings[0], /attach\.json is invalid/);
  w.chrome.__state.files["attach.json"] = ATTACH;
  assert.equal((await until(() => h.FakeWebSocket.last())).url, "ws://127.0.0.1:47800", "retried");
});

// ── identity and handshake ───────────────────────────────────────────────

test("attach hello: sent only after the challenge; proof/tokenProof are HMACs; no secret on the wire", async (t) => {
  const storage = {};
  const w = makeWorker(t, { storage });
  const ws = await until(() => h.FakeWebSocket.last());
  ws.open();
  await sleep(20);
  assert.deepEqual(ws.sent, [], "the server speaks first");

  const serverNonce = newNonce();
  ws.serverSend({ type: "challenge", protocol: 2, serverNonce });
  const hello = await until(() => ws.sent[0]);
  assert.match(hello.installId, UUID_RE);
  assert.match(hello.clientNonce, NONCE_RE);
  assert.equal(Buffer.from(hello.clientNonce, "base64url").length, 32);
  assert.deepEqual(hello, {
    type: "hello",
    protocol: 2,
    mode: "attach",
    installId: hello.installId,
    clientNonce: hello.clientNonce,
    proof: mac("enr-secret", `emunium-client|${serverNonce}|${hello.clientNonce}|${hello.installId}`),
    tokenProof: null,
    extensionVersion: "3.1.0",
    userAgent: "test-agent/1.0",
  });
  assert.ok(!ws.rawSent.join("\n").includes("enr-secret"));
  assert.equal(w.chrome.__state.storage.local.installId, hello.installId);

  const [a, b] = await Promise.all([w.bg.loadConfig(), w.bg.loadConfig()]);
  assert.equal(a.installId, hello.installId);
  assert.equal(b.installId, hello.installId);

  // A restarted worker in the same profile reuses the stored id.
  const again = makeWorker(t, { storage: w.chrome.__state.storage.local });
  const ws2 = await until(() => h.FakeWebSocket.last());
  const { hello: hello2 } = await challenge(ws2);
  assert.equal(hello2.installId, hello.installId);
  assert.notEqual(hello2.clientNonce, hello.clientNonce, "fresh nonce per connection");
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

test("a verified welcome's token is stored; the next hello proves it (tokenProof) without sending it", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.backoffMinMs = 5;
  const ws = await until(() => h.FakeWebSocket.last());
  const { hello } = await handshake(w, ws, { token: "tok-new" });
  await until(() => w.chrome.__state.storage.local.token === "tok-new");

  ws.serverClose(1006);
  const ws2 = await until(() => h.FakeWebSocket.instances[1]);
  const hs2 = await challenge(ws2);
  assert.equal(hs2.hello.installId, hello.installId);
  assert.equal(hs2.hello.tokenProof, mac("tok-new", `emunium-token|${hs2.serverNonce}|${hs2.hello.clientNonce}`));
  assert.ok(!("token" in hs2.hello) && !("enrollment" in hs2.hello));
  assert.ok(!ws2.rawSent.join("\n").includes("tok-new"));
  await welcome(w, ws2, hs2);
  await h.tick();
  assert.equal(w.chrome.__state.storage.local.token, "tok-new", "a token-less welcome keeps it");

  const launch = makeWorker(t, { files: { "session.json": SESSION } });
  const ws3 = await until(() => h.FakeWebSocket.last());
  ws3.open();
  assert.deepEqual(ws3.sent[0], { type: "hello", protocol: 2, token: "tok-abc", extensionVersion: "3.1.0", userAgent: "test-agent/1.0" });
  ws3.serverSend({ type: "welcome", protocol: 2, token: "ignored" });
  assert.equal(launch.bg.isConnected(), true, "launch mode: plain token hello, sync welcome");
  await h.tick();
  assert.equal(launch.chrome.storage.local.set.calls.length, 0, "launch mode stores nothing");
});

test("squatter without the secret: nothing secret leaves, no request runs, token unchanged, 4005 + backoff", async (t) => {
  const w = makeWorker(t, { storage: { installId: INSTALL_ID, token: "tok-old" } });
  w.bg.timing.keepAliveMs = 5;
  const handled = h.spy(() => {});
  w.bg.handleRequest = handled;
  commit(w.chrome, { url: CLAIM_URL });
  await until(() => w.bg.state.pendingClaims.length === 1);

  const ws = await until(() => h.FakeWebSocket.last());
  const { hello, serverNonce } = await challenge(ws);
  w.bg.state.backoffMs = 4000;
  // The squatter cannot compute the proof; reflecting the client's own proof must not work either.
  ws.serverSend({ type: "welcome", protocol: 2, installId: hello.installId, serverProof: hello.proof, token: "tok-evil" });
  ws.serverSend({ type: "request", id: 1, method: "evaluate", params: { expression: "1" } });
  await until(() => ws.closeCalls.length);
  await sleep(30);
  assert.deepEqual(closeCodes(ws), [4005]);
  assert.equal(handled.calls.length, 0, "no request executed");
  assert.equal(w.bg.isConnected(), false);
  assert.deepEqual(ws.sent.map((f) => f.type), ["hello"], "no claim, reply or ping");
  assert.equal(w.bg.state.pendingClaims.length, 1, "claim kept for the real hub");
  assert.equal(w.chrome.__state.storage.local.token, "tok-old");
  assert.ok(!w.chrome.storage.local.set.calls.some(([items]) => items && "token" in items), "no token written");
  const raw = ws.rawSent.join("\n");
  assert.ok(!raw.includes("enr-secret") && !raw.includes("tok-old"));
  assert.equal(hello.tokenProof, mac("tok-old", `emunium-token|${serverNonce}|${hello.clientNonce}`));
  assert.ok(w.bg.state.reconnectTimer, "reconnect scheduled");
  assert.equal(w.bg.state.backoffMs, 8000, "backed off");

  // An old-style squatter that never challenges and just welcomes gets nothing at all.
  const w2 = makeWorker(t, { storage: { installId: INSTALL_ID, token: "tok-old" } });
  const ws2 = await until(() => h.FakeWebSocket.last());
  ws2.open();
  ws2.serverSend({ type: "welcome", protocol: 2, token: "tok-evil" });
  assert.deepEqual(closeCodes(ws2), [4005]);
  assert.deepEqual(ws2.rawSent, []);
  assert.equal(w2.chrome.__state.storage.local.token, "tok-old");
});

test("wrong or missing serverProof closes 4005; only the verified welcome stores a token", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.backoffMinMs = 5;
  w.bg.timing.attachBackoffMaxMs = 10;
  const bad = [
    () => undefined,
    () => "",
    () => 42,
    (hello, sN) => serverProofFor(hello, sN, "wrong-secret"),
    (hello, sN) => serverProofFor(Object.assign({}, hello, { clientNonce: newNonce() }), sN),
    (hello, sN) => serverProofFor(hello, newNonce()),
    (hello, sN) => mac("enr-secret", `emunium-client|${sN}|${hello.clientNonce}|${hello.installId}`),
    (hello, sN) => {
      const bytes = Buffer.from(serverProofFor(hello, sN), "base64url");
      bytes[31] ^= 1; // differs only in the last byte
      return bytes.toString("base64url");
    },
    (hello, sN) => Buffer.from(serverProofFor(hello, sN), "base64url").subarray(0, 31).toString("base64url"),
    (hello, sN) => Buffer.from(serverProofFor(hello, sN), "base64url").toString("base64"), // padded, not base64url
    (hello, sN) => serverProofFor(hello, sN) + "AA",
  ];
  for (let i = 0; i < bad.length; i += 1) {
    const ws = await until(() => h.FakeWebSocket.instances[i]);
    const { hello, serverNonce } = await challenge(ws);
    ws.serverSend({ type: "welcome", protocol: 2, installId: hello.installId, serverProof: bad[i](hello, serverNonce), token: "tok-evil" });
    await until(() => ws.closeCalls.length, 1500);
    assert.deepEqual(closeCodes(ws), [4005], `variant ${i}`);
    assert.equal(w.bg.isConnected(), false);
  }
  assert.equal(w.chrome.__state.storage.local.token, undefined, "no token from an unverified welcome");

  // A welcome for another installId is rejected even with a proof over it.
  const wsX = await until(() => h.FakeWebSocket.instances[bad.length]);
  const hsX = await challenge(wsX);
  const other = Object.assign({}, hsX.hello, { installId: INSTALL_ID });
  wsX.serverSend({ type: "welcome", protocol: 2, installId: INSTALL_ID, serverProof: serverProofFor(other, hsX.serverNonce), token: "tok-evil" });
  await until(() => wsX.closeCalls.length);
  assert.deepEqual(closeCodes(wsX), [4005]);

  const ws = await until(() => h.FakeWebSocket.instances[bad.length + 1]);
  await handshake(w, ws, { token: "tok-good" });
  await until(() => w.chrome.__state.storage.local.token === "tok-good");
  assert.ok(!w.chrome.storage.local.set.calls.some(([items]) => items && items.token === "tok-evil"));
  assert.equal(w.bg.state.backoffMs, w.bg.timing.backoffMinMs, "a verified welcome resets the backoff");
});

test("challenge: missing (5 s timeout) or malformed first frame closes 4005 before anything is sent", async (t) => {
  const w = makeWorker(t);
  assert.equal(w.bg.timing.challengeTimeoutMs, 5000, "default 5 s");
  w.bg.timing.challengeTimeoutMs = 30;
  w.bg.timing.backoffMinMs = 5;
  w.bg.timing.attachBackoffMaxMs = 10;
  const firstFrames = [
    null, // no frame at all: timeout
    "not json",
    { type: "welcome", protocol: 2 },
    { type: "challenge", protocol: 1, serverNonce: newNonce() },
    { type: "challenge", protocol: 2 },
    { type: "challenge", protocol: 2, serverNonce: "short" },
    { type: "challenge", protocol: 2, serverNonce: newNonce() + "=" },
    { type: "challenge", protocol: 2, serverNonce: nodeCrypto.randomBytes(33).toString("base64url") },
  ];
  for (let i = 0; i < firstFrames.length; i += 1) {
    // Each failure backs off (5-10 ms here) and the worker reconnects on a new socket.
    const ws = await until(() => h.FakeWebSocket.instances[i]);
    ws.open();
    if (firstFrames[i] !== null) {
      ws.serverSend(firstFrames[i]);
    }
    await until(() => ws.closeCalls.length, 1000);
    assert.deepEqual(closeCodes(ws), [4005], `case ${i}`);
    assert.deepEqual(ws.rawSent, [], `case ${i}: nothing sent`);
    assert.equal(w.bg.isConnected(), false);
  }
});

test("requests sent right after a verified welcome run once verified, in order", async (t) => {
  const w = makeWorker(t);
  const handled = h.spy(() => {});
  w.bg.handleRequest = handled;
  const ws = await until(() => h.FakeWebSocket.last());
  const hs = await challenge(ws);
  ws.serverSend({ type: "welcome", protocol: 2, installId: hs.hello.installId, serverProof: serverProofFor(hs.hello, hs.serverNonce) });
  ws.serverSend({ type: "request", id: 1, method: "a" });
  ws.serverSend({ type: "request", id: 2, method: "b" });
  assert.equal(handled.calls.length, 0, "nothing runs before verification");
  await until(() => handled.calls.length === 2);
  assert.deepEqual(handled.calls.map(([, frame]) => frame.id), [1, 2]);
  ws.serverSend({ type: "request", id: 3, method: "c" });
  assert.equal(handled.calls.length, 3);
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
  assert.equal(bg.hubPortOf("ws://127.0.0.1"), null, "no implicit port 80");
});

test("claim: a commit before the welcome is queued, sent only after the verified welcome, tab blanked", async (t) => {
  const w = makeWorker(t);
  // Fired before loadConfig resolved: the worker reads the files itself.
  commit(w.chrome, { url: CLAIM_URL });
  await until(() => blanked(w.chrome).length === 1);
  assert.deepEqual(plain(blanked(w.chrome)[0]), [1, { url: "about:blank" }]);

  const ws = await until(() => h.FakeWebSocket.last());
  const hs = await challenge(ws);
  await sleep(20);
  assert.deepEqual(
    ws.sent.map((f) => f.type),
    ["hello"],
    "nothing but hello on a socket that is not welcomed"
  );
  await welcome(w, ws, hs, { token: "tok" });
  assert.deepEqual(claims(ws), [{ type: "event", event: "claim", nonce: NONCE }]);
  assert.equal(w.bg.state.pendingClaims.length, 0);

  // Same nonce again (reload before about:blank landed): ignored.
  commit(w.chrome, { url: CLAIM_URL });
  await sleep(20);
  assert.equal(claims(ws).length, 1);
  assert.equal(blanked(w.chrome).length, 1);
});

test("claim queue holds at most 16 nonces; the oldest are dropped", async (t) => {
  const w = makeWorker(t);
  const nonces = Array.from({ length: 20 }, (_, i) => `cap${String(i).padStart(2, "0")}`.padEnd(20, "x"));
  for (let i = 0; i < nonces.length; i += 1) {
    commit(w.chrome, { url: `http://127.0.0.1:47800/pair#n=${nonces[i]}` });
    await until(() => blanked(w.chrome).length === i + 1);
  }
  assert.deepEqual(plain(w.bg.state.pendingClaims.map((c) => c.nonce)), nonces.slice(4));

  const ws = await until(() => h.FakeWebSocket.last());
  await handshake(w, ws);
  assert.deepEqual(claims(ws).map((c) => c.nonce), nonces.slice(4));
  assert.equal(w.bg.state.pendingClaims.length, 0);
});

test("claim: sent at once when welcomed; queued across a reconnect; never on the unwelcomed socket", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.backoffMinMs = 5;
  const ws = await until(() => h.FakeWebSocket.last());
  await handshake(w, ws);
  commit(w.chrome, { url: CLAIM_URL });
  await until(() => claims(ws).length === 1);

  ws.serverClose(1006);
  const second = `http://127.0.0.1:47800/pair#n=${"q".repeat(40)}`;
  commit(w.chrome, { url: second });
  await until(() => blanked(w.chrome).length === 2);
  const ws2 = await until(() => h.FakeWebSocket.instances[1]);
  const hs2 = await challenge(ws2);
  await sleep(10);
  assert.equal(claims(ws2).length, 0);
  assert.equal(ws.droppedSent.length, 0);
  await welcome(w, ws2, hs2);
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
  await handshake(w, ws);
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

test("claim: a claim tab open before the worker loaded is queued at start, blanked, sent after the welcome", async (t) => {
  const other = `http://127.0.0.1:47801/pair#n=${"p".repeat(20)}`;
  const w = makeWorker(t, {
    before: (chrome) => {
      // Committed while the worker was still starting: no onCommitted is ever emitted for it.
      chrome.__state.tabs.get(1).url = CLAIM_URL;
      chrome.__state.tabs.set(2, { id: 2, windowId: 1, index: 1, active: false, url: other, status: "complete" });
      chrome.__state.tabs.set(3, { id: 3, windowId: 1, index: 2, active: false, url: CLAIM_URL, status: "complete" });
    },
  });
  await until(() => blanked(w.chrome).length === 1);
  assert.deepEqual(plain(blanked(w.chrome)[0]), [1, { url: "about:blank" }], "same nonce in tab 3 is not queued twice");
  assert.equal(w.bg.state.pendingClaims.length, 1);

  const ws = await until(() => h.FakeWebSocket.last());
  const hs = await challenge(ws);
  await sleep(10);
  assert.equal(claims(ws).length, 0, "never on the unwelcomed socket");
  await welcome(w, ws, hs);
  assert.deepEqual(claims(ws), [{ type: "event", event: "claim", nonce: NONCE }]);

  // The welcome rescan and a late commit of the same nonce add nothing.
  commit(w.chrome, { url: CLAIM_URL, tabId: 3 });
  await sleep(20);
  assert.equal(claims(ws).length, 1);
  assert.equal(blanked(w.chrome).length, 1);
});

test("claim: every attach welcome rescans open tabs; launch mode never scans", async (t) => {
  const w = makeWorker(t);
  w.bg.timing.backoffMinMs = 5;
  const ws = await until(() => h.FakeWebSocket.last());
  await handshake(w, ws);
  await sleep(10);
  assert.equal(claims(ws).length, 0);

  // A claim page that no onCommitted reported (e.g. lost while the worker was suspended).
  const late = "w".repeat(32);
  w.chrome.__state.tabs.get(1).url = `http://127.0.0.1:47800/pair#n=${late}`;
  ws.serverClose(1006);
  const ws2 = await until(() => h.FakeWebSocket.instances[1]);
  await handshake(w, ws2);
  await until(() => claims(ws2).length === 1);
  assert.deepEqual(claims(ws2), [{ type: "event", event: "claim", nonce: late }]);
  assert.deepEqual(plain(blanked(w.chrome)), [[1, { url: "about:blank" }]]);

  const launch = makeWorker(t, {
    files: { "session.json": SESSION, "attach.json": ATTACH },
    before: (chrome) => {
      chrome.__state.tabs.get(1).url = CLAIM_URL;
    },
  });
  const lws = await until(() => h.FakeWebSocket.last());
  lws.open();
  lws.serverSend({ type: "welcome", protocol: 2 });
  await sleep(20);
  assert.equal(launch.chrome.tabs.query.calls.length, 0, "launch mode does not query tabs");
  assert.equal(claims(lws).length, 0);
  assert.equal(blanked(launch.chrome).length, 0);
});

test("manifest: storage permission and minimum_chrome_version 116", () => {
  const manifest = require("node:fs").readFileSync(require("node:path").join(h.EXTENSION_DIR, "manifest.json"), "utf8");
  const parsed = JSON.parse(manifest);
  assert.ok(parsed.permissions.includes("storage"));
  assert.equal(parsed.minimum_chrome_version, "116");
});
