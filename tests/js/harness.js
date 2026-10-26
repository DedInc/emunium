"use strict";
/**
 * tests/js/harness.js - dependency-free fakes for unit-testing the MV3 extension
 * (emunium/extension/*.js) under Node 18+. CommonJS:
 *   const h = require("./harness");
 * Self-check: node tests/js/harness.js --self-test
 *
 * ── Generic ────────────────────────────────────────────────────────────────
 * REPO_ROOT, EXTENSION_DIR           absolute paths.
 * spy(impl?) -> fn                   calls impl; records fn.calls (argument arrays);
 *                                    fn.impl is swappable; fn.reset() clears calls.
 * tick(n = 3) -> Promise             lets n macrotask turns run (settles promise chains).
 *
 * ── Fake chrome ────────────────────────────────────────────────────────────
 * createFakeChrome(options?) -> chrome
 *   options: {tabs, windows, files, manifest, extensionId}
 *     tabs     default [{id:1, windowId:1, index:0, active:true, url:"https://example.test/",
 *              title:"Example", status:"complete", zoom:1}]
 *     windows  default [{id:1, focused:true, type:"normal", state:"normal"}]
 *     files    {"session.json": {...}} served by the default fetch (objects -> JSON)
 *   Namespaces: runtime, tabs, windows, scripting, webNavigation, webRequest, userScripts, storage.
 *   Methods are plain properties holding async functions (spies): replace per test, e.g.
 *     chrome.tabs.sendMessage = h.spy(async (tabId, msg, opts) => ({ok: true, result: null}));
 *   Defaults keep an in-memory model in chrome.__state {tabs: Map, windows: Map,
 *   lastFocusedWindowId, files, scripts}. Defaults accept a trailing callback (sets
 *   chrome.runtime.lastError while it runs); wrap your own with h.makeApi(chrome, impl).
 *   tabs.sendMessage rejects "Could not establish connection. Receiving end does not exist."
 *   tabs.getZoom resolves tab.zoom (default 1); scripting.executeScript / userScripts.execute
 *   resolve [{frameId:0, documentId, result:undefined}] and never run the function.
 *   Events (ChromeEvent): addListener(fn, ...extra) (deduplicated), removeListener(fn),
 *   hasListener(fn), hasListeners(), listeners, entries ({fn, extra}), emit(...args) ->
 *   listener return values (a throwing listener throws out of emit), clear().
 * dispatchMessage(event, message, sender = {}, {timeoutMs = 1000}?) -> Promise<response>
 *   chrome.runtime.onMessage semantics: listeners get (message, sender, sendResponse);
 *   returning true keeps the channel open; returned promises are NOT responses (Chrome);
 *   resolves undefined when nobody answers synchronously; rejects on timeout.
 * createFakeFetch(chrome, routes = {}) -> fetch   records fetch.calls. Serves
 *   chrome-extension://<id>/<path> from chrome.__state.files (query ignored, 404 if missing),
 *   then routes[url] (value | (url, init) => value | {status, body}); other URLs reject
 *   TypeError("Failed to fetch"). Responses: {ok, status, url, headers.get, json(), text()}.
 *
 * ── Fake WebSocket ─────────────────────────────────────────────────────────
 * FakeWebSocket(url)                 CONNECTING/OPEN/CLOSING/CLOSED (static + instance),
 *   readyState, url, sent (parsed frames; non-JSON kept as strings), rawSent, droppedSent
 *   (sent while CLOSING/CLOSED), closeCalls, onopen/onmessage/onclose/onerror +
 *   addEventListener/removeEventListener. send() while CONNECTING throws InvalidStateError.
 *   ws.open()                        -> OPEN, fires "open".
 *   ws.serverSend(obj | string)      fires "message" {data} (objects JSON-encoded).
 *   ws.serverClose(code=1000, reason="")  -> CLOSED, fires "close" {code, reason, wasClean}.
 *   ws.fail()                        fires "error" then close 1006.
 *   ws.close(code, reason)           client close; dispatches "close" synchronously.
 *   FakeWebSocket.instances, FakeWebSocket.last(), FakeWebSocket.reset().
 *
 * ── Script loading ─────────────────────────────────────────────────────────
 * loadScripts(files, globals = {}, options = {baseDir: EXTENSION_DIR, trackTimers: true}) -> ctx
 *   Contextifies `globals` IN PLACE (ctx === globals, so tests read ctx.EmuniumBackground
 *   and mutate globals live) and runs each file (relative to baseDir) in it. Missing globals
 *   get defaults: console, timers, queueMicrotask, URL, URLSearchParams, TextEncoder/Decoder,
 *   structuredClone, AbortController, atob/btoa, performance, crypto, self, chrome
 *   (createFakeChrome()), WebSocket (FakeWebSocket), fetch (createFakeFetch(chrome)),
 *   importScripts(...names) (loads names relative to baseDir). No `window` is added: pass
 *   createWindow(...) for content scripts. Calling loadScripts again with the same ctx runs
 *   more files in it (re-injection). ctx.__harness = {baseDir, loaded, timers, dispose(),
 *   evaluate(code)}; dispose() clears every timer created inside the context.
 *
 * ── Fake DOM ───────────────────────────────────────────────────────────────
 * createDocument({url, title, readyState}?) -> document with documentElement/head/body.
 *   document: createElement, getElementById, querySelector(All), contains, getRootNode,
 *   activeElement, hasFocus() (document.__hasFocus), visibilityState, readyState, title,
 *   elementFromPoint(x,y)/elementsFromPoint (assign a function or call
 *   setElementFromPoint(elOrFn); default hit-tests element rects, last in tree order wins,
 *   skipping display:none / visibility:hidden / pointer-events:none), createTreeWalker(root,
 *   whatToShow, filter) over elements only (nextNode, currentNode; filter fn or
 *   {acceptNode}; FILTER_REJECT skips subtrees), evaluate() throws NotSupportedError unless
 *   assigned, add/removeEventListener, dispatchEvent.
 *   h.el(document, tag, attrs?, children?|text?) builds trees quickly.
 * Elements: nodeType 1, tagName/nodeName (upper), localName, id, className, classList
 *   (contains/add/remove/toggle), attributes (iterable [{name, value}]),
 *   get/set/has/removeAttribute, getAttributeNames, toggleAttribute, children, childNodes,
 *   parentNode, parentElement, siblings, append/appendChild/insertBefore/removeChild/remove/
 *   replaceChildren, isConnected, contains, getRootNode, matches, closest,
 *   querySelector(All) (arrays), textContent (own text + children; set replaces children),
 *   innerText (el.__innerText overrides), getBoundingClientRect (el.setRect({x,y,width,height})
 *   or el.__rect; assigning the method also works), getClientRects, offsetWidth/offsetHeight/
 *   clientWidth/clientHeight (rect-derived; assignable), scrollWidth/scrollHeight/scrollTop/
 *   scrollLeft, style (setProperty/getPropertyValue), el.__computedStyle (object or
 *   el => object) read by getComputedStyle, disabled (reflects the attribute), value (form
 *   controls only; undefined elsewhere), checked, type, name, isContentEditable, focus()
 *   (no-op when disabled / el.__focusable === false / detached; fires blur/focusout,
 *   focus/focusin; records focusCalls), blur(), click(), scrollIntoView(arg) (records
 *   scrollIntoViewCalls, then calls el.__onScrollIntoView(arg) if set), shadowRoot null.
 *   No text nodes, shadow DOM, layout or XPath.
 * Selectors: tag, *, #id, .class, [attr], [attr=v|"v"|'v'] (+ ~= |= ^= $= *= and " i"),
 *   compounds, descendant " " and child ">" combinators, comma lists. Invalid syntax throws
 *   {name:"SyntaxError", code:12}; pseudo-classes, "+" and "~" throw NotSupportedError.
 * Events: DomEvent (Event), FakeMouseEvent, FakePointerEvent, FakeKeyboardEvent,
 *   FakeFocusEvent, FakeCustomEvent; init keys are copied onto the event (isTrusted, clientX,
 *   timeStamp...). Dispatch runs capture (window -> target) then bubble (if bubbles), honours
 *   once/stopPropagation/stopImmediatePropagation/preventDefault, and rethrows the first
 *   listener error after dispatch finishes.
 *
 * createWindow(document?, overrides = {}) -> window-like global for loadScripts
 *   window/self/document, screenX/screenY(+screenLeft/Top), outerWidth 1280, outerHeight 800,
 *   innerWidth 1280, innerHeight 712, devicePixelRatio 1, visualViewport {offsetLeft,
 *   offsetTop, pageLeft, pageTop, width, height, scale}, scrollX/scrollY (+pageX/YOffset),
 *   scrollTo(x,y | {left,top}) / scroll / scrollBy (record scrollCalls), location (from
 *   overrides.url, default https://example.test/), navigator, performance {timeOrigin, now}
 *   (Node clock), add/removeEventListener/dispatchEvent (end of the event path),
 *   getComputedStyle(el), MutationObserver (per-window class: instances, triggerAll(records);
 *   observer.trigger(records)), triggerMutations(records), timers, requestAnimationFrame,
 *   WeakRef, crypto {randomUUID, getRandomValues}, NodeFilter, Node, XPathResult, Element,
 *   HTMLElement, Document, Event classes. `overrides` are assigned last.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const nodeCrypto = require("crypto");
const assert = require("assert");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EXTENSION_DIR = path.join(REPO_ROOT, "emunium", "extension");

// ─── generic helpers ──────────────────────────────────────────────────────

function spy(impl) {
  const fn = function (...args) {
    fn.calls.push(args);
    return fn.impl ? fn.impl.apply(this, args) : undefined;
  };
  fn.calls = [];
  fn.impl = impl || null;
  fn.reset = () => {
    fn.calls.length = 0;
  };
  return fn;
}

function tick(turns = 3) {
  let chain = Promise.resolve();
  for (let i = 0; i < turns; i += 1) {
    chain = chain.then(() => new Promise((resolve) => setImmediate(resolve)));
  }
  return chain;
}

function makeDomError(name, message, code) {
  const error = new Error(message);
  error.name = name;
  if (code !== undefined) {
    error.code = code;
  }
  return error;
}

function copy(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

// ─── fake chrome ──────────────────────────────────────────────────────────

class ChromeEvent {
  constructor(name) {
    this.name = name || "event";
    this._entries = [];
  }

  addListener(fn, ...extra) {
    if (typeof fn !== "function") {
      throw new TypeError(`${this.name}.addListener requires a function`);
    }
    if (!this.hasListener(fn)) {
      this._entries.push({ fn, extra });
    }
  }

  removeListener(fn) {
    this._entries = this._entries.filter((entry) => entry.fn !== fn);
  }

  hasListener(fn) {
    return this._entries.some((entry) => entry.fn === fn);
  }

  hasListeners() {
    return this._entries.length > 0;
  }

  get listeners() {
    return this._entries.map((entry) => entry.fn);
  }

  get entries() {
    return this._entries.slice();
  }

  emit(...args) {
    return this._entries.slice().map((entry) => entry.fn(...args));
  }

  clear() {
    this._entries = [];
  }
}

function dispatchMessage(event, message, sender = {}, { timeoutMs = 1000 } = {}) {
  return new Promise((resolve, reject) => {
    let done = false;
    let open = false;
    let timer = null;
    const finish = (error, value) => {
      if (done) {
        return;
      }
      done = true;
      if (timer) {
        clearTimeout(timer);
      }
      if (error) {
        reject(error);
      } else {
        resolve(value);
      }
    };
    const sendResponse = (value) => finish(null, value);
    for (const fn of event.listeners) {
      let returned;
      try {
        returned = fn(message, sender, sendResponse);
      } catch (error) {
        finish(error);
        return;
      }
      if (returned === true) {
        open = true;
      }
    }
    if (!open) {
      finish(null, undefined);
      return;
    }
    timer = setTimeout(
      () => finish(new Error(`${event.name}: no sendResponse within ${timeoutMs} ms`)),
      timeoutMs
    );
  });
}

function makeApi(chrome, impl) {
  return spy(function (...args) {
    let callback = null;
    if (args.length && typeof args[args.length - 1] === "function") {
      callback = args.pop();
    }
    let promise;
    try {
      promise = Promise.resolve(impl.apply(this, args));
    } catch (error) {
      promise = Promise.reject(error);
    }
    if (!callback) {
      return promise;
    }
    promise.then(
      (value) => callback(value),
      (error) => {
        chrome.runtime.lastError = { message: String((error && error.message) || error) };
        try {
          callback(undefined);
        } finally {
          chrome.runtime.lastError = undefined;
        }
      }
    );
    return undefined;
  });
}

function createFakeChrome(options = {}) {
  const extensionId = options.extensionId || "fakeextensionid";
  const state = {
    tabs: new Map(),
    windows: new Map(),
    lastFocusedWindowId: null,
    nextTabId: 1,
    nextWindowId: 1,
    files: Object.assign({}, options.files || {}),
    scripts: [],
    storage: { local: {}, session: {} },
  };
  const chrome = { __state: state };
  const api = (impl) => makeApi(chrome, impl);
  const events = (prefix, names) => {
    const out = {};
    for (const name of names) {
      out[name] = new ChromeEvent(`${prefix}.${name}`);
    }
    return out;
  };

  const windows = options.windows || [{ id: 1, focused: true, type: "normal", state: "normal" }];
  for (const win of windows) {
    const record = Object.assign({ focused: false, type: "normal", state: "normal" }, win);
    state.windows.set(record.id, record);
    state.nextWindowId = Math.max(state.nextWindowId, record.id + 1);
    if (record.focused || state.lastFocusedWindowId === null) {
      state.lastFocusedWindowId = record.id;
    }
  }
  const tabs = options.tabs || [
    {
      id: 1,
      windowId: 1,
      index: 0,
      active: true,
      url: "https://example.test/",
      title: "Example",
      status: "complete",
    },
  ];
  for (const tab of tabs) {
    const record = Object.assign(
      { windowId: state.lastFocusedWindowId, active: false, status: "complete", zoom: 1 },
      tab
    );
    state.tabs.set(record.id, record);
    state.nextTabId = Math.max(state.nextTabId, record.id + 1);
  }

  const resolveWindowId = (id) => (id === -2 ? state.lastFocusedWindowId : id);
  const getTab = (tabId) => {
    const tab = state.tabs.get(tabId);
    if (!tab) {
      throw new Error(`No tab with id: ${tabId}.`);
    }
    return tab;
  };
  const tabView = (tab) => {
    const view = copy(tab);
    delete view.zoom;
    return view;
  };
  const windowView = (win, populate) => {
    const view = copy(win);
    if (populate) {
      view.tabs = [...state.tabs.values()].filter((t) => t.windowId === win.id).map(tabView);
    }
    return view;
  };
  const activate = (tab) => {
    for (const other of state.tabs.values()) {
      if (other.windowId === tab.windowId) {
        other.active = other === tab;
      }
    }
  };

  chrome.runtime = Object.assign(
    {
      id: extensionId,
      lastError: undefined,
      getURL: (p) => `chrome-extension://${extensionId}/${String(p).replace(/^\/+/, "")}`,
      getManifest: () =>
        Object.assign({ manifest_version: 3, name: "emunium", version: "3.1.0" }, options.manifest),
      sendMessage: api(async () => undefined),
      reload: spy(),
    },
    events("runtime", ["onMessage", "onInstalled", "onStartup", "onSuspend", "onConnect"])
  );

  chrome.tabs = Object.assign(
    {
      TAB_ID_NONE: -1,
      query: api(async (info = {}) => {
        const wanted = info.windowId === undefined ? undefined : resolveWindowId(info.windowId);
        return [...state.tabs.values()]
          .filter((tab) => info.active === undefined || tab.active === info.active)
          .filter((tab) => wanted === undefined || tab.windowId === wanted)
          .filter(
            (tab) =>
              !(info.lastFocusedWindow || info.currentWindow) ||
              tab.windowId === state.lastFocusedWindowId
          )
          .filter((tab) => info.status === undefined || tab.status === info.status)
          .filter((tab) => info.url === undefined || tab.url === info.url)
          .filter((tab) => {
            if (!info.windowType) {
              return true;
            }
            const win = state.windows.get(tab.windowId);
            return win && win.type === info.windowType;
          })
          .map(tabView);
      }),
      get: api(async (tabId) => tabView(getTab(tabId))),
      create: api(async (props = {}) => {
        const windowId =
          props.windowId === undefined ? state.lastFocusedWindowId : resolveWindowId(props.windowId);
        const tab = {
          id: state.nextTabId++,
          windowId,
          index: [...state.tabs.values()].filter((t) => t.windowId === windowId).length,
          active: props.active !== false,
          url: props.url || "about:blank",
          title: "",
          status: "loading",
          zoom: 1,
        };
        state.tabs.set(tab.id, tab);
        if (tab.active) {
          activate(tab);
        }
        chrome.tabs.onCreated.emit(tabView(tab));
        return tabView(tab);
      }),
      update: api(async (tabId, props = {}) => {
        const tab = getTab(tabId);
        const change = {};
        if (props.url !== undefined) {
          tab.url = props.url;
          tab.status = "loading";
          change.url = props.url;
          change.status = "loading";
        }
        if (props.active) {
          activate(tab);
          chrome.tabs.onActivated.emit({ tabId: tab.id, windowId: tab.windowId });
        }
        chrome.tabs.onUpdated.emit(tab.id, change, tabView(tab));
        return tabView(tab);
      }),
      remove: api(async (tabIds) => {
        for (const tabId of [].concat(tabIds)) {
          const tab = getTab(tabId);
          state.tabs.delete(tabId);
          chrome.tabs.onRemoved.emit(tabId, { windowId: tab.windowId, isWindowClosing: false });
        }
      }),
      reload: api(async () => undefined),
      sendMessage: api(async () => {
        throw new Error("Could not establish connection. Receiving end does not exist.");
      }),
      getZoom: api(async (tabId) => {
        const tab = getTab(tabId);
        return tab.zoom === undefined ? 1 : tab.zoom;
      }),
      setZoom: api(async (tabId, zoom) => {
        const tab = getTab(tabId);
        const oldZoomFactor = tab.zoom;
        tab.zoom = zoom;
        chrome.tabs.onZoomChange.emit({ tabId, oldZoomFactor, newZoomFactor: zoom });
      }),
    },
    events("tabs", ["onCreated", "onUpdated", "onRemoved", "onActivated", "onReplaced", "onZoomChange"])
  );

  chrome.windows = Object.assign(
    {
      WINDOW_ID_NONE: -1,
      WINDOW_ID_CURRENT: -2,
      get: api(async (windowId, info = {}) => {
        const win = state.windows.get(resolveWindowId(windowId));
        if (!win) {
          throw new Error(`No window with id: ${windowId}.`);
        }
        return windowView(win, info.populate);
      }),
      getAll: api(async (info = {}) =>
        [...state.windows.values()]
          .filter((win) => !info.windowTypes || info.windowTypes.includes(win.type))
          .map((win) => windowView(win, info.populate))
      ),
      getLastFocused: api(async (info = {}) => {
        const win = state.windows.get(state.lastFocusedWindowId);
        return win ? windowView(win, info.populate) : undefined;
      }),
      getCurrent: api(async (info = {}) => {
        const win = state.windows.get(state.lastFocusedWindowId);
        return win ? windowView(win, info.populate) : undefined;
      }),
      update: api(async (windowId, props = {}) => {
        const win = state.windows.get(windowId);
        if (!win) {
          throw new Error(`No window with id: ${windowId}.`);
        }
        if (props.focused) {
          for (const other of state.windows.values()) {
            other.focused = other === win;
          }
          state.lastFocusedWindowId = win.id;
          chrome.windows.onFocusChanged.emit(win.id);
        }
        if (props.state) {
          win.state = props.state;
        }
        return windowView(win, false);
      }),
      create: api(async (props = {}) => {
        const win = { id: state.nextWindowId++, focused: props.focused !== false, type: props.type || "normal", state: "normal" };
        state.windows.set(win.id, win);
        if (win.focused) {
          state.lastFocusedWindowId = win.id;
        }
        await chrome.tabs.create({ windowId: win.id, url: props.url || "about:blank", active: true });
        chrome.windows.onCreated.emit(windowView(win, false));
        return windowView(win, true);
      }),
      remove: api(async (windowId) => {
        state.windows.delete(windowId);
        for (const tab of [...state.tabs.values()]) {
          if (tab.windowId === windowId) {
            state.tabs.delete(tab.id);
            chrome.tabs.onRemoved.emit(tab.id, { windowId, isWindowClosing: true });
          }
        }
        chrome.windows.onRemoved.emit(windowId);
      }),
    },
    events("windows", ["onCreated", "onRemoved", "onFocusChanged", "onBoundsChanged"])
  );

  const injectionResult = (injection = {}) => {
    const tabId = injection.target ? injection.target.tabId : undefined;
    const frameIds = (injection.target && injection.target.frameIds) || [0];
    return frameIds.map((frameId) => ({ frameId, documentId: `doc-${tabId}-${frameId}`, result: undefined }));
  };

  chrome.scripting = {
    executeScript: api(async (injection) => injectionResult(injection)),
    insertCSS: api(async () => undefined),
    removeCSS: api(async () => undefined),
    registerContentScripts: api(async (scripts) => {
      state.scripts.push(...copy(scripts));
    }),
    getRegisteredContentScripts: api(async () => copy(state.scripts)),
    unregisterContentScripts: api(async (filter = {}) => {
      state.scripts = filter.ids ? state.scripts.filter((s) => !filter.ids.includes(s.id)) : [];
    }),
  };

  chrome.userScripts = {
    __scripts: [],
    __worlds: [],
    register: api(async (scripts) => {
      chrome.userScripts.__scripts.push(...copy(scripts));
    }),
    update: api(async () => undefined),
    unregister: api(async (filter = {}) => {
      chrome.userScripts.__scripts = filter.ids
        ? chrome.userScripts.__scripts.filter((s) => !filter.ids.includes(s.id))
        : [];
    }),
    getScripts: api(async () => copy(chrome.userScripts.__scripts)),
    configureWorld: api(async (config) => {
      chrome.userScripts.__worlds.push(copy(config));
    }),
    resetWorldConfiguration: api(async () => undefined),
    execute: api(async (injection) => injectionResult(injection)),
  };

  chrome.webNavigation = Object.assign(
    {
      getFrame: api(async ({ tabId, frameId = 0 }) => {
        const tab = state.tabs.get(tabId);
        return tab
          ? { frameId, parentFrameId: -1, url: tab.url, documentId: `doc-${tabId}-${frameId}`, errorOccurred: false }
          : null;
      }),
      getAllFrames: api(async ({ tabId }) => {
        const frame = await chrome.webNavigation.getFrame({ tabId, frameId: 0 });
        return frame ? [frame] : null;
      }),
    },
    events("webNavigation", [
      "onBeforeNavigate",
      "onCommitted",
      "onDOMContentLoaded",
      "onCompleted",
      "onErrorOccurred",
      "onHistoryStateUpdated",
      "onReferenceFragmentUpdated",
    ])
  );

  chrome.webRequest = events("webRequest", [
    "onBeforeRequest",
    "onBeforeSendHeaders",
    "onSendHeaders",
    "onHeadersReceived",
    "onResponseStarted",
    "onBeforeRedirect",
    "onCompleted",
    "onErrorOccurred",
  ]);

  const area = (name) => ({
    get: api(async (keys) => {
      const store = state.storage[name];
      if (keys === null || keys === undefined) {
        return copy(store);
      }
      const out = {};
      if (typeof keys === "string" || Array.isArray(keys)) {
        for (const key of [].concat(keys)) {
          if (key in store) {
            out[key] = copy(store[key]);
          }
        }
        return out;
      }
      for (const [key, fallback] of Object.entries(keys)) {
        out[key] = key in store ? copy(store[key]) : fallback;
      }
      return out;
    }),
    set: api(async (items) => {
      Object.assign(state.storage[name], copy(items));
    }),
    remove: api(async (keys) => {
      for (const key of [].concat(keys)) {
        delete state.storage[name][key];
      }
    }),
    clear: api(async () => {
      state.storage[name] = {};
    }),
  });
  chrome.storage = Object.assign(
    { local: area("local"), session: area("session") },
    events("storage", ["onChanged"])
  );

  return chrome;
}

function fakeResponse(url, status, body) {
  const text = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : status === 404 ? "Not Found" : "",
    url,
    headers: { get: (name) => (String(name).toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => JSON.parse(text),
    text: async () => text,
  };
}

function createFakeFetch(chrome, routes = {}) {
  const fetch = spy(async (input, init) => {
    const url = String(input && input.url ? input.url : input);
    const bare = url.split(/[?#]/)[0];
    const origin = chrome && chrome.runtime ? chrome.runtime.getURL("") : null;
    if (origin && bare.startsWith(origin)) {
      const files = chrome.__state.files;
      const name = decodeURIComponent(bare.slice(origin.length));
      let value = files[name];
      if (typeof value === "function") {
        value = value(url, init);
      }
      return value === undefined ? fakeResponse(url, 404, "") : fakeResponse(url, 200, value);
    }
    if (Object.prototype.hasOwnProperty.call(routes, bare) || Object.prototype.hasOwnProperty.call(routes, url)) {
      let value = Object.prototype.hasOwnProperty.call(routes, url) ? routes[url] : routes[bare];
      if (typeof value === "function") {
        value = await value(url, init);
      }
      if (value && typeof value === "object" && "status" in value && "body" in value) {
        return fakeResponse(url, value.status, value.body);
      }
      return fakeResponse(url, 200, value);
    }
    throw new TypeError("Failed to fetch");
  });
  return fetch;
}

// ─── fake WebSocket ───────────────────────────────────────────────────────

class FakeWebSocket {
  constructor(url, protocols) {
    this.url = String(url);
    this.protocols = protocols;
    this.protocol = "";
    this.extensions = "";
    this.binaryType = "blob";
    this.bufferedAmount = 0;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    this.rawSent = [];
    this.droppedSent = [];
    this.closeCalls = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    this._listeners = new Map();
    FakeWebSocket.instances.push(this);
  }

  static last() {
    return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  }

  static reset() {
    FakeWebSocket.instances.length = 0;
  }

  addEventListener(type, fn) {
    const list = this._listeners.get(type) || [];
    if (!list.includes(fn)) {
      list.push(fn);
    }
    this._listeners.set(type, list);
  }

  removeEventListener(type, fn) {
    this._listeners.set(type, (this._listeners.get(type) || []).filter((l) => l !== fn));
  }

  _fire(type, props = {}) {
    const event = Object.assign({ type, target: this, currentTarget: this }, props);
    const handler = this["on" + type];
    if (typeof handler === "function") {
      handler.call(this, event);
    }
    for (const fn of (this._listeners.get(type) || []).slice()) {
      if (typeof fn === "function") {
        fn.call(this, event);
      } else if (fn && typeof fn.handleEvent === "function") {
        fn.handleEvent(event);
      }
    }
    return event;
  }

  send(data) {
    if (this.readyState === FakeWebSocket.CONNECTING) {
      throw makeDomError(
        "InvalidStateError",
        "Failed to execute 'send' on 'WebSocket': Still in CONNECTING state.",
        11
      );
    }
    const parsed = parseFrame(data);
    if (this.readyState !== FakeWebSocket.OPEN) {
      this.droppedSent.push(parsed);
      return;
    }
    this.rawSent.push(data);
    this.sent.push(parsed);
  }

  open() {
    if (this.readyState !== FakeWebSocket.CONNECTING) {
      throw new Error(`FakeWebSocket.open(): readyState is ${this.readyState}, not CONNECTING`);
    }
    this.readyState = FakeWebSocket.OPEN;
    this._fire("open");
    return this;
  }

  serverSend(obj) {
    if (this.readyState !== FakeWebSocket.OPEN) {
      throw new Error(`FakeWebSocket.serverSend(): socket is not OPEN (readyState ${this.readyState})`);
    }
    const data = typeof obj === "string" ? obj : JSON.stringify(obj);
    this._fire("message", { data });
    return this;
  }

  serverClose(code = 1000, reason = "") {
    if (this.readyState === FakeWebSocket.CLOSED) {
      return this;
    }
    this.readyState = FakeWebSocket.CLOSED;
    this._fire("close", { code, reason, wasClean: code !== 1006 });
    return this;
  }

  fail() {
    if (this.readyState === FakeWebSocket.CLOSED) {
      return this;
    }
    this._fire("error");
    return this.serverClose(1006, "");
  }

  close(code, reason = "") {
    this.closeCalls.push({ code, reason });
    if (code !== undefined && code !== 1000 && !(code >= 3000 && code <= 4999)) {
      throw makeDomError("InvalidAccessError", `The close code must be either 1000, or between 3000 and 4999. ${code} is neither.`, 15);
    }
    if (this.readyState === FakeWebSocket.CLOSING || this.readyState === FakeWebSocket.CLOSED) {
      return;
    }
    if (this.readyState === FakeWebSocket.CONNECTING) {
      this.readyState = FakeWebSocket.CLOSED;
      this._fire("error");
      this._fire("close", { code: 1006, reason: "", wasClean: false });
      return;
    }
    this.readyState = FakeWebSocket.CLOSED;
    this._fire("close", { code: code === undefined ? 1005 : code, reason, wasClean: true });
  }
}

const WS_STATES = { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 };
for (const [name, value] of Object.entries(WS_STATES)) {
  Object.defineProperty(FakeWebSocket, name, { value, enumerable: true });
  Object.defineProperty(FakeWebSocket.prototype, name, { value, enumerable: true });
}
FakeWebSocket.instances = [];

function parseFrame(data) {
  if (typeof data !== "string") {
    return data;
  }
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

// ─── script loading ───────────────────────────────────────────────────────

function installTimerTracking(sandbox) {
  const timers = { timeouts: new Set(), intervals: new Set() };
  const baseSetTimeout = sandbox.setTimeout;
  const baseClearTimeout = sandbox.clearTimeout || clearTimeout;
  const baseSetInterval = sandbox.setInterval;
  const baseClearInterval = sandbox.clearInterval || clearInterval;
  sandbox.setTimeout = function (fn, ms, ...args) {
    const handle = baseSetTimeout(
      (...inner) => {
        timers.timeouts.delete(handle);
        if (typeof fn === "function") {
          fn(...inner);
        }
      },
      ms,
      ...args
    );
    timers.timeouts.add(handle);
    return handle;
  };
  sandbox.clearTimeout = function (handle) {
    timers.timeouts.delete(handle);
    return baseClearTimeout(handle);
  };
  sandbox.setInterval = function (fn, ms, ...args) {
    const handle = baseSetInterval(fn, ms, ...args);
    timers.intervals.add(handle);
    return handle;
  };
  sandbox.clearInterval = function (handle) {
    timers.intervals.delete(handle);
    return baseClearInterval(handle);
  };
  timers.dispose = () => {
    for (const handle of timers.timeouts) {
      baseClearTimeout(handle);
    }
    for (const handle of timers.intervals) {
      baseClearInterval(handle);
    }
    timers.timeouts.clear();
    timers.intervals.clear();
  };
  return timers;
}

function loadScripts(files, globals = {}, options = {}) {
  const sandbox = globals;
  if (!vm.isContext(sandbox)) {
    const baseDir = options.baseDir || EXTENSION_DIR;
    const defaults = {
      console,
      setTimeout,
      clearTimeout,
      setInterval,
      clearInterval,
      queueMicrotask,
      structuredClone,
      URL,
      URLSearchParams,
      TextEncoder,
      TextDecoder,
      AbortController,
      AbortSignal,
      atob,
      btoa,
      performance,
      crypto: { randomUUID: () => nodeCrypto.randomUUID(), getRandomValues: (a) => nodeCrypto.getRandomValues(a) },
    };
    for (const [key, value] of Object.entries(defaults)) {
      if (!(key in sandbox)) {
        sandbox[key] = value;
      }
    }
    if (!("chrome" in sandbox)) {
      sandbox.chrome = createFakeChrome();
    }
    if (!("WebSocket" in sandbox)) {
      sandbox.WebSocket = FakeWebSocket;
    }
    if (!("fetch" in sandbox)) {
      sandbox.fetch = createFakeFetch(sandbox.chrome);
    }
    if (!("self" in sandbox)) {
      sandbox.self = sandbox;
    }
    const timers = options.trackTimers === false ? null : installTimerTracking(sandbox);
    const harness = {
      baseDir,
      loaded: [],
      timers,
      dispose: () => timers && timers.dispose(),
      evaluate: (code, filename = "harness-eval.js") => vm.runInContext(code, sandbox, { filename }),
    };
    Object.defineProperty(sandbox, "__harness", { value: harness, enumerable: false, configurable: true });
    if (!("importScripts" in sandbox)) {
      sandbox.importScripts = (...names) => {
        for (const name of names) {
          runScriptFile(sandbox, name);
        }
      };
    }
    vm.createContext(sandbox, { name: "emunium-harness" });
  }
  for (const file of [].concat(files || [])) {
    runScriptFile(sandbox, file);
  }
  return sandbox;
}

function runScriptFile(sandbox, file) {
  const harness = sandbox.__harness;
  const fullPath = path.isAbsolute(file) ? file : path.join(harness.baseDir, file);
  const code = fs.readFileSync(fullPath, "utf8");
  harness.loaded.push(fullPath);
  vm.runInContext(code, sandbox, { filename: fullPath });
}

// ─── fake DOM: selectors ──────────────────────────────────────────────────

const IDENT_RE = /^(?:--|-?(?:[A-Za-z_ -￿]|\\.))(?:[\w\- -￿]|\\.)*/;
const selectorCache = new Map();

function parseSelectorList(text) {
  const source = String(text);
  if (selectorCache.has(source)) {
    return selectorCache.get(source);
  }
  const invalid = () =>
    makeDomError("SyntaxError", `Failed to execute 'querySelector': '${source}' is not a valid selector.`, 12);
  const unsupported = (what) =>
    makeDomError("NotSupportedError", `fake DOM does not support ${what} in selector '${source}'`, 9);
  let i = 0;
  const n = source.length;
  const skipWs = () => {
    const start = i;
    while (i < n && /\s/.test(source[i])) {
      i += 1;
    }
    return i > start;
  };
  const readIdent = () => {
    const match = IDENT_RE.exec(source.slice(i));
    if (!match) {
      throw invalid();
    }
    i += match[0].length;
    return match[0].replace(/\\(.)/g, "$1");
  };
  const readQuoted = () => {
    const quote = source[i];
    i += 1;
    let out = "";
    while (i < n && source[i] !== quote) {
      if (source[i] === "\\" && i + 1 < n) {
        i += 1;
      }
      out += source[i];
      i += 1;
    }
    if (i >= n) {
      throw invalid();
    }
    i += 1;
    return out;
  };
  const parseCompound = () => {
    const compound = { tag: null, id: null, classes: [], attrs: [] };
    let any = false;
    if (source[i] === "*") {
      i += 1;
      any = true;
    } else if (IDENT_RE.test(source.slice(i))) {
      compound.tag = readIdent().toLowerCase();
      any = true;
    }
    for (;;) {
      const ch = source[i];
      if (ch === "#") {
        i += 1;
        compound.id = readIdent();
      } else if (ch === ".") {
        i += 1;
        compound.classes.push(readIdent());
      } else if (ch === "[") {
        i += 1;
        skipWs();
        const name = readIdent().toLowerCase();
        skipWs();
        if (source[i] === "]") {
          i += 1;
          compound.attrs.push({ name, op: null, value: null, insensitive: false });
        } else {
          const opMatch = /^[~|^$*]?=/.exec(source.slice(i));
          if (!opMatch) {
            throw invalid();
          }
          i += opMatch[0].length;
          skipWs();
          const value = source[i] === '"' || source[i] === "'" ? readQuoted() : readIdent();
          skipWs();
          let insensitive = false;
          if (/[iIsS]/.test(source[i] || "") && source[i + 1] !== undefined) {
            insensitive = source[i].toLowerCase() === "i";
            i += 1;
            skipWs();
          }
          if (source[i] !== "]") {
            throw invalid();
          }
          i += 1;
          compound.attrs.push({ name, op: opMatch[0], value, insensitive });
        }
      } else if (ch === ":") {
        throw unsupported("pseudo-classes");
      } else {
        break;
      }
      any = true;
    }
    if (!any) {
      throw invalid();
    }
    return compound;
  };

  const groups = [];
  skipWs();
  if (i >= n) {
    throw invalid();
  }
  for (;;) {
    const parts = [{ combinator: null, compound: parseCompound() }];
    for (;;) {
      const hadWs = skipWs();
      const ch = source[i];
      if (i >= n || ch === ",") {
        break;
      }
      if (ch === "+" || ch === "~") {
        throw unsupported(`the '${ch}' combinator`);
      }
      let combinator = " ";
      if (ch === ">") {
        combinator = ">";
        i += 1;
        skipWs();
      } else if (!hadWs) {
        throw invalid();
      }
      parts.push({ combinator, compound: parseCompound() });
    }
    groups.push(parts);
    if (i >= n) {
      break;
    }
    i += 1; // ','
    skipWs();
    if (i >= n) {
      throw invalid();
    }
  }
  selectorCache.set(source, groups);
  return groups;
}

function matchAttr(element, attr) {
  let actual = element.getAttribute(attr.name);
  if (actual === null) {
    return false;
  }
  if (!attr.op) {
    return true;
  }
  let expected = attr.value;
  if (attr.insensitive) {
    actual = actual.toLowerCase();
    expected = expected.toLowerCase();
  }
  switch (attr.op) {
    case "=":
      return actual === expected;
    case "~=":
      return expected !== "" && actual.split(/\s+/).includes(expected);
    case "|=":
      return actual === expected || actual.startsWith(expected + "-");
    case "^=":
      return expected !== "" && actual.startsWith(expected);
    case "$=":
      return expected !== "" && actual.endsWith(expected);
    case "*=":
      return expected !== "" && actual.includes(expected);
    default:
      return false;
  }
}

function matchCompound(element, compound) {
  if (compound.tag && element.localName !== compound.tag) {
    return false;
  }
  if (compound.id !== null && element.getAttribute("id") !== compound.id) {
    return false;
  }
  for (const cls of compound.classes) {
    if (!element.classList.contains(cls)) {
      return false;
    }
  }
  return compound.attrs.every((attr) => matchAttr(element, attr));
}

function matchComplex(element, parts, index = parts.length - 1) {
  if (!matchCompound(element, parts[index].compound)) {
    return false;
  }
  if (index === 0) {
    return true;
  }
  if (parts[index].combinator === ">") {
    const parent = element.parentElement;
    return !!parent && matchComplex(parent, parts, index - 1);
  }
  for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (matchComplex(ancestor, parts, index - 1)) {
      return true;
    }
  }
  return false;
}

function matchesSelector(element, selector) {
  return parseSelectorList(selector).some((parts) => matchComplex(element, parts));
}

function descendants(node, out = []) {
  for (const child of node.__children) {
    out.push(child);
    descendants(child, out);
  }
  return out;
}

// ─── fake DOM: events ─────────────────────────────────────────────────────

const RESERVED_EVENT_KEYS = new Set(["type", "target", "currentTarget", "eventPhase", "defaultPrevented"]);

class DomEvent {
  constructor(type, init = {}) {
    this.type = String(type);
    this.bubbles = !!init.bubbles;
    this.cancelable = !!init.cancelable;
    this.composed = !!init.composed;
    this.isTrusted = false;
    this.timeStamp = performance.now();
    this.target = null;
    this.currentTarget = null;
    this.eventPhase = 0;
    this.defaultPrevented = false;
    this.__stop = false;
    this.__stopImmediate = false;
    this.__path = [];
    for (const [key, value] of Object.entries(init || {})) {
      if (!RESERVED_EVENT_KEYS.has(key)) {
        this[key] = value;
      }
    }
  }

  preventDefault() {
    if (this.cancelable && !this.__inPassive) {
      this.defaultPrevented = true;
    }
  }

  stopPropagation() {
    this.__stop = true;
  }

  stopImmediatePropagation() {
    this.__stop = true;
    this.__stopImmediate = true;
  }

  composedPath() {
    return this.__path.slice();
  }
}
Object.assign(DomEvent, { NONE: 0, CAPTURING_PHASE: 1, AT_TARGET: 2, BUBBLING_PHASE: 3 });

function setDefaults(target, defaults) {
  for (const [key, value] of Object.entries(defaults)) {
    if (target[key] === undefined) {
      target[key] = value;
    }
  }
}

const MODIFIER_DEFAULTS = { ctrlKey: false, shiftKey: false, altKey: false, metaKey: false };

class FakeMouseEvent extends DomEvent {
  constructor(type, init = {}) {
    super(type, init);
    setDefaults(this, Object.assign({
      clientX: 0, clientY: 0, screenX: 0, screenY: 0, pageX: 0, pageY: 0, offsetX: 0, offsetY: 0,
      movementX: 0, movementY: 0, button: 0, buttons: 0, detail: 0, relatedTarget: null,
    }, MODIFIER_DEFAULTS));
    this.x = this.clientX;
    this.y = this.clientY;
  }
}

class FakePointerEvent extends FakeMouseEvent {
  constructor(type, init = {}) {
    super(type, init);
    setDefaults(this, { pointerId: 1, pointerType: "mouse", isPrimary: true, width: 1, height: 1, pressure: 0 });
  }
}

class FakeKeyboardEvent extends DomEvent {
  constructor(type, init = {}) {
    super(type, init);
    setDefaults(this, Object.assign({ key: "", code: "", location: 0, repeat: false, isComposing: false }, MODIFIER_DEFAULTS));
  }
}

class FakeFocusEvent extends DomEvent {
  constructor(type, init = {}) {
    super(type, init);
    setDefaults(this, { relatedTarget: null });
  }
}

class FakeCustomEvent extends DomEvent {
  constructor(type, init = {}) {
    super(type, init);
    if (this.detail === undefined) {
      this.detail = null;
    }
  }
}

function listenerOptions(options) {
  if (typeof options === "boolean") {
    return { capture: options, once: false, passive: false };
  }
  const o = options || {};
  return { capture: !!o.capture, once: !!o.once, passive: !!o.passive };
}

const eventTargetMethods = {
  addEventListener(type, fn, options) {
    if (!fn) {
      return;
    }
    const opts = listenerOptions(options);
    const map = this.__listeners;
    const list = map.get(type) || [];
    if (!list.some((l) => l.fn === fn && l.capture === opts.capture)) {
      list.push(Object.assign({ fn }, opts));
    }
    map.set(type, list);
  },
  removeEventListener(type, fn, options) {
    const { capture } = listenerOptions(options);
    const list = this.__listeners.get(type) || [];
    this.__listeners.set(type, list.filter((l) => !(l.fn === fn && l.capture === capture)));
  },
  dispatchEvent(event) {
    return dispatchDomEvent(this, event);
  },
};

function installEventTarget(target) {
  Object.defineProperty(target, "__listeners", { value: new Map(), enumerable: false, writable: true });
  for (const [name, fn] of Object.entries(eventTargetMethods)) {
    if (!(name in target)) {
      Object.defineProperty(target, name, { value: fn, enumerable: false, writable: true, configurable: true });
    }
  }
}

function eventParent(node) {
  if (node && node.nodeType === 1) {
    return node.parentNode;
  }
  if (node && node.nodeType === 9) {
    return node.defaultView || null;
  }
  return null;
}

function dispatchDomEvent(target, event) {
  if (!event || typeof event.type !== "string") {
    throw new TypeError("dispatchEvent requires an Event");
  }
  const path = [];
  for (let node = target; node; node = eventParent(node)) {
    path.push(node);
  }
  event.target = target;
  event.__path = path.slice();
  event.__stop = false;
  event.__stopImmediate = false;
  const errors = [];
  const invoke = (node, phase, explicit) => {
    const listeners = explicit || (node.__listeners ? (node.__listeners.get(event.type) || []).slice() : []);
    event.eventPhase = phase;
    event.currentTarget = node;
    for (const listener of listeners) {
      if (phase === 1 && !listener.capture) continue;
      if (phase === 3 && listener.capture) continue;
      if (listener.once) {
        node.removeEventListener(event.type, listener.fn, listener);
      }
      event.__inPassive = listener.passive;
      try {
        if (typeof listener.fn === "function") {
          listener.fn.call(node, event);
        } else if (listener.fn && typeof listener.fn.handleEvent === "function") {
          listener.fn.handleEvent(event);
        }
      } catch (error) {
        errors.push(error);
      }
      event.__inPassive = false;
      if (event.__stopImmediate) {
        break;
      }
    }
  };
  for (let index = path.length - 1; index > 0 && !event.__stop; index -= 1) {
    invoke(path[index], 1);
  }
  if (!event.__stop) {
    // At target: capture listeners first, then non-capture ones (Chrome 89+).
    const node = path[0];
    const listeners = node.__listeners ? node.__listeners.get(event.type) || [] : [];
    const ordered = listeners.filter((l) => l.capture).concat(listeners.filter((l) => !l.capture));
    invoke(node, 2, ordered);
  }
  if (event.bubbles) {
    for (let index = 1; index < path.length && !event.__stop; index += 1) {
      invoke(path[index], 3);
    }
  }
  event.eventPhase = 0;
  event.currentTarget = null;
  if (errors.length) {
    throw errors[0];
  }
  return !event.defaultPrevented;
}

// ─── fake DOM: nodes ──────────────────────────────────────────────────────

const FORM_VALUE_TAGS = new Set(["input", "textarea", "select", "option", "button"]);
const NODE_CONSTANTS = {
  ELEMENT_NODE: 1, ATTRIBUTE_NODE: 2, TEXT_NODE: 3, COMMENT_NODE: 8, DOCUMENT_NODE: 9,
  DOCUMENT_FRAGMENT_NODE: 11,
  DOCUMENT_POSITION_DISCONNECTED: 1, DOCUMENT_POSITION_PRECEDING: 2, DOCUMENT_POSITION_FOLLOWING: 4,
  DOCUMENT_POSITION_CONTAINS: 8, DOCUMENT_POSITION_CONTAINED_BY: 16,
};
const NODE_FILTER = {
  FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3,
  SHOW_ALL: 0xffffffff, SHOW_ELEMENT: 0x1, SHOW_ATTRIBUTE: 0x2, SHOW_TEXT: 0x4, SHOW_COMMENT: 0x80,
  SHOW_DOCUMENT: 0x100,
};
const XPATH_RESULT = {
  ANY_TYPE: 0, NUMBER_TYPE: 1, STRING_TYPE: 2, BOOLEAN_TYPE: 3, UNORDERED_NODE_ITERATOR_TYPE: 4,
  ORDERED_NODE_ITERATOR_TYPE: 5, UNORDERED_NODE_SNAPSHOT_TYPE: 6, ORDERED_NODE_SNAPSHOT_TYPE: 7,
  ANY_UNORDERED_NODE_TYPE: 8, FIRST_ORDERED_NODE_TYPE: 9,
};
const DEFAULT_COMPUTED_STYLE = {
  display: "block", visibility: "visible", opacity: "1", pointerEvents: "auto",
  position: "static", overflow: "visible", cursor: "auto", transform: "none", zIndex: "auto",
};

function toCamel(name) {
  const text = String(name);
  return text.startsWith("--") ? text : text.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

function createStyleDeclaration(initial) {
  const style = {};
  Object.defineProperties(style, {
    setProperty: { value(name, value) { this[toCamel(name)] = value === null ? "" : String(value); } },
    getPropertyValue: {
      value(name) {
        const value = this[toCamel(name)];
        return value === undefined || value === null ? "" : String(value);
      },
    },
    removeProperty: {
      value(name) {
        const key = toCamel(name);
        const old = this.getPropertyValue(key);
        delete this[key];
        return old;
      },
    },
  });
  for (const [key, value] of Object.entries(initial || {})) {
    style[toCamel(key)] = value;
  }
  return style;
}

function computedStyleOf(element) {
  const own = typeof element.__computedStyle === "function" ? element.__computedStyle(element) : element.__computedStyle;
  const merged = {};
  for (const source of [DEFAULT_COMPUTED_STYLE, element.style, own || {}]) {
    for (const [key, value] of Object.entries(source)) {
      if (value !== "" && value !== undefined) {
        merged[toCamel(key)] = String(value);
      }
    }
  }
  return createStyleDeclaration(merged);
}

function rectFrom(raw) {
  const r = Object.assign({ x: 0, y: 0, width: 0, height: 0 }, raw || {});
  return {
    x: r.x, y: r.y, width: r.width, height: r.height,
    top: r.y, left: r.x, right: r.x + r.width, bottom: r.y + r.height,
    toJSON() {
      return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.y, left: r.x, right: r.x + r.width, bottom: r.y + r.height };
    },
  };
}

class FakeElement {
  constructor(ownerDocument, tagName) {
    installEventTarget(this);
    this.ownerDocument = ownerDocument;
    this.localName = String(tagName).toLowerCase();
    this.tagName = this.localName.toUpperCase();
    this.nodeName = this.tagName;
    this.namespaceURI = "http://www.w3.org/1999/xhtml";
    this.parentNode = null;
    this.shadowRoot = null;
    this.__children = [];
    this.__attrs = [];
    this.__text = "";
    this.__rect = { x: 0, y: 0, width: 0, height: 0 };
    this.__computedStyle = {};
    this.style = createStyleDeclaration();
    this.scrollIntoViewCalls = [];
    this.focusCalls = [];
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.scrollWidth = 0;
    this.scrollHeight = 0;
  }

  get nodeType() {
    return 1;
  }

  // attributes
  getAttribute(name) {
    const attr = this.__attrs.find((a) => a.name === String(name).toLowerCase());
    return attr ? attr.value : null;
  }

  setAttribute(name, value) {
    const key = String(name).toLowerCase();
    if (!IDENT_RE.test(key) && !/^[a-z_:][\w:.-]*$/.test(key)) {
      throw makeDomError("InvalidCharacterError", `'${name}' is not a valid attribute name.`, 5);
    }
    const attr = this.__attrs.find((a) => a.name === key);
    if (attr) {
      attr.value = String(value);
    } else {
      this.__attrs.push({ name: key, value: String(value) });
    }
  }

  hasAttribute(name) {
    return this.getAttribute(name) !== null;
  }

  removeAttribute(name) {
    const key = String(name).toLowerCase();
    this.__attrs = this.__attrs.filter((a) => a.name !== key);
  }

  toggleAttribute(name, force) {
    const present = this.hasAttribute(name);
    const want = force === undefined ? !present : !!force;
    if (want && !present) {
      this.setAttribute(name, "");
    } else if (!want && present) {
      this.removeAttribute(name);
    }
    return want;
  }

  getAttributeNames() {
    return this.__attrs.map((a) => a.name);
  }

  get attributes() {
    const list = this.__attrs.map((a) => ({ name: a.name, value: a.value }));
    list.item = (i) => list[i] || null;
    list.getNamedItem = (name) => list.find((a) => a.name === String(name).toLowerCase()) || null;
    return list;
  }

  get id() {
    return this.getAttribute("id") || "";
  }

  set id(value) {
    this.setAttribute("id", value);
  }

  get className() {
    return this.getAttribute("class") || "";
  }

  set className(value) {
    this.setAttribute("class", value);
  }

  get name() {
    return this.getAttribute("name") || "";
  }

  set name(value) {
    this.setAttribute("name", value);
  }

  get type() {
    const value = this.getAttribute("type");
    if (value !== null) {
      return value.toLowerCase();
    }
    if (this.localName === "input") return "text";
    if (this.localName === "button") return "submit";
    return "";
  }

  set type(value) {
    this.setAttribute("type", value);
  }

  get classList() {
    const element = this;
    const tokens = () => element.className.split(/\s+/).filter(Boolean);
    const write = (list) => element.setAttribute("class", list.join(" "));
    return {
      contains: (token) => tokens().includes(token),
      add: (...add) => write([...new Set(tokens().concat(add))]),
      remove: (...remove) => write(tokens().filter((t) => !remove.includes(t))),
      toggle: (token, force) => {
        const has = tokens().includes(token);
        const want = force === undefined ? !has : !!force;
        if (want && !has) write(tokens().concat([token]));
        if (!want && has) write(tokens().filter((t) => t !== token));
        return want;
      },
      item: (i) => tokens()[i] || null,
      get length() {
        return tokens().length;
      },
      [Symbol.iterator]: () => tokens()[Symbol.iterator](),
    };
  }

  get disabled() {
    return this.hasAttribute("disabled");
  }

  set disabled(value) {
    this.toggleAttribute("disabled", !!value);
  }

  get checked() {
    return this.__checked !== undefined ? this.__checked : this.hasAttribute("checked");
  }

  set checked(value) {
    this.__checked = !!value;
  }

  get value() {
    if (!FORM_VALUE_TAGS.has(this.localName)) {
      return this.__value;
    }
    if (this.__value !== undefined) {
      return this.__value;
    }
    if (this.localName === "textarea") {
      return this.textContent;
    }
    return this.getAttribute("value") || "";
  }

  set value(value) {
    this.__value = FORM_VALUE_TAGS.has(this.localName) ? String(value) : value;
  }

  get isContentEditable() {
    for (let node = this; node && node.nodeType === 1; node = node.parentNode) {
      const value = node.getAttribute("contenteditable");
      if (value !== null) {
        return value === "" || value.toLowerCase() === "true" || value.toLowerCase() === "plaintext-only";
      }
    }
    return false;
  }

  // tree
  get children() {
    return this.__children.slice();
  }

  get childNodes() {
    return this.__children.slice();
  }

  get childElementCount() {
    return this.__children.length;
  }

  get firstElementChild() {
    return this.__children[0] || null;
  }

  get lastElementChild() {
    return this.__children[this.__children.length - 1] || null;
  }

  get parentElement() {
    return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null;
  }

  get nextElementSibling() {
    const siblings = this.parentNode ? this.parentNode.__children : [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  get previousElementSibling() {
    const siblings = this.parentNode ? this.parentNode.__children : [];
    const index = siblings.indexOf(this);
    return index > 0 ? siblings[index - 1] : null;
  }

  appendChild(child) {
    return this.insertBefore(child, null);
  }

  insertBefore(child, reference) {
    if (!child || child.nodeType !== 1) {
      throw new TypeError("fake DOM only inserts elements; use textContent for text");
    }
    if (child.contains(this)) {
      throw makeDomError("HierarchyRequestError", "The new child element contains the parent.", 3);
    }
    if (reference && reference.parentNode !== this) {
      throw makeDomError("NotFoundError", "The node before which the new node is to be inserted is not a child of this node.", 8);
    }
    if (child.parentNode) {
      child.parentNode.removeChild(child);
    }
    const index = reference ? this.__children.indexOf(reference) : this.__children.length;
    this.__children.splice(index, 0, child);
    child.parentNode = this;
    return child;
  }

  removeChild(child) {
    const index = this.__children.indexOf(child);
    if (index < 0) {
      throw makeDomError("NotFoundError", "The node to be removed is not a child of this node.", 8);
    }
    this.__children.splice(index, 1);
    child.parentNode = null;
    const doc = this.ownerDocument;
    if (doc && doc.activeElement && child.contains(doc.activeElement)) {
      doc.activeElement = doc.body;
    }
    return child;
  }

  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }

  append(...nodes) {
    for (const node of nodes) {
      if (node && node.nodeType === 1) {
        this.appendChild(node);
      } else {
        this.__text += String(node);
      }
    }
  }

  replaceChildren(...nodes) {
    for (const child of this.__children.slice()) {
      this.removeChild(child);
    }
    this.__text = "";
    this.append(...nodes);
  }

  getRootNode() {
    let node = this;
    while (node.parentNode) {
      node = node.parentNode;
    }
    return node;
  }

  get isConnected() {
    const root = this.getRootNode();
    return !!root && root.nodeType === 9;
  }

  contains(other) {
    for (let node = other; node; node = node.parentNode) {
      if (node === this) {
        return true;
      }
    }
    return false;
  }

  matches(selector) {
    return matchesSelector(this, selector);
  }

  closest(selector) {
    for (let node = this; node && node.nodeType === 1; node = node.parentNode) {
      if (matchesSelector(node, selector)) {
        return node;
      }
    }
    return null;
  }

  querySelectorAll(selector) {
    const groups = parseSelectorList(selector);
    return descendants(this).filter((el) => groups.some((parts) => matchComplex(el, parts)));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  getElementsByTagName(tag) {
    const name = String(tag).toLowerCase();
    return descendants(this).filter((el) => name === "*" || el.localName === name);
  }

  // text
  get textContent() {
    return this.__text + this.__children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    for (const child of this.__children.slice()) {
      this.removeChild(child);
    }
    this.__text = value === null || value === undefined ? "" : String(value);
  }

  get innerText() {
    return this.__innerText !== undefined ? this.__innerText : this.textContent;
  }

  set innerText(value) {
    this.__innerText = undefined;
    this.textContent = value;
  }

  // geometry
  getBoundingClientRect() {
    return rectFrom(typeof this.__rect === "function" ? this.__rect(this) : this.__rect);
  }

  setRect(rect) {
    this.__rect = Object.assign({ x: 0, y: 0, width: 0, height: 0 }, rect);
    this.__rectSet = true;
    return this;
  }

  getClientRects() {
    const rect = this.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0 ? [rect] : [];
  }

  get offsetWidth() {
    return this.__offsetWidth !== undefined ? this.__offsetWidth : Math.round(this.getBoundingClientRect().width);
  }

  set offsetWidth(value) {
    this.__offsetWidth = value;
  }

  get offsetHeight() {
    return this.__offsetHeight !== undefined ? this.__offsetHeight : Math.round(this.getBoundingClientRect().height);
  }

  set offsetHeight(value) {
    this.__offsetHeight = value;
  }

  get clientWidth() {
    return this.__clientWidth !== undefined ? this.__clientWidth : this.offsetWidth;
  }

  set clientWidth(value) {
    this.__clientWidth = value;
  }

  get clientHeight() {
    return this.__clientHeight !== undefined ? this.__clientHeight : this.offsetHeight;
  }

  set clientHeight(value) {
    this.__clientHeight = value;
  }

  scrollIntoView(arg) {
    this.scrollIntoViewCalls.push(arg === undefined ? null : arg);
    if (typeof this.__onScrollIntoView === "function") {
      this.__onScrollIntoView(arg);
    }
  }

  // interaction
  focus(options) {
    this.focusCalls.push(options === undefined ? null : options);
    const doc = this.ownerDocument;
    if (!doc || this.disabled || this.__focusable === false || !this.isConnected) {
      return;
    }
    const previous = doc.activeElement;
    if (previous === this) {
      return;
    }
    doc.activeElement = this;
    if (previous && previous !== doc.body && previous.isConnected) {
      previous.dispatchEvent(new FakeFocusEvent("blur", { relatedTarget: this }));
      previous.dispatchEvent(new FakeFocusEvent("focusout", { bubbles: true, relatedTarget: this }));
    }
    this.dispatchEvent(new FakeFocusEvent("focus", { relatedTarget: previous || null }));
    this.dispatchEvent(new FakeFocusEvent("focusin", { bubbles: true, relatedTarget: previous || null }));
  }

  blur() {
    const doc = this.ownerDocument;
    if (!doc || doc.activeElement !== this) {
      return;
    }
    doc.activeElement = doc.body;
    this.dispatchEvent(new FakeFocusEvent("blur"));
    this.dispatchEvent(new FakeFocusEvent("focusout", { bubbles: true }));
  }

  click() {
    if (this.disabled) {
      return;
    }
    this.dispatchEvent(new FakeMouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
  }
}
Object.assign(FakeElement, NODE_CONSTANTS);

class FakeTreeWalker {
  constructor(root, whatToShow, filter) {
    this.root = root;
    this.whatToShow = whatToShow === undefined ? NODE_FILTER.SHOW_ALL : whatToShow >>> 0;
    this.filter = filter || null;
    this.currentNode = root;
  }

  _accept(node) {
    if (!(this.whatToShow & NODE_FILTER.SHOW_ELEMENT)) {
      return NODE_FILTER.FILTER_SKIP;
    }
    if (!this.filter) {
      return NODE_FILTER.FILTER_ACCEPT;
    }
    const verdict = typeof this.filter === "function" ? this.filter(node) : this.filter.acceptNode(node);
    return verdict === undefined ? NODE_FILTER.FILTER_ACCEPT : verdict;
  }

  nextNode() {
    let node = this.currentNode;
    let verdict = NODE_FILTER.FILTER_ACCEPT;
    for (;;) {
      while (verdict !== NODE_FILTER.FILTER_REJECT && node.__children.length) {
        node = node.__children[0];
        verdict = this._accept(node);
        if (verdict === NODE_FILTER.FILTER_ACCEPT) {
          this.currentNode = node;
          return node;
        }
      }
      let sibling = null;
      for (let probe = node; probe && probe !== this.root; probe = probe.parentNode) {
        const siblings = probe.parentNode ? probe.parentNode.__children : [];
        sibling = siblings[siblings.indexOf(probe) + 1] || null;
        if (sibling) {
          break;
        }
      }
      if (!sibling) {
        return null;
      }
      node = sibling;
      verdict = this._accept(node);
      if (verdict === NODE_FILTER.FILTER_ACCEPT) {
        this.currentNode = node;
        return node;
      }
    }
  }
}

class FakeDocument {
  constructor(options = {}) {
    installEventTarget(this);
    this.nodeName = "#document";
    this.parentNode = null;
    this.ownerDocument = null;
    this.defaultView = null;
    this.__children = [];
    this.documentElement = this.createElement("html");
    this.documentElement.parentNode = this;
    this.__children.push(this.documentElement);
    this.head = this.createElement("head");
    this.body = this.createElement("body");
    this.documentElement.append(this.head, this.body);
    this.title = options.title || "";
    this.readyState = options.readyState || "complete";
    this.visibilityState = "visible";
    this.hidden = false;
    this.URL = options.url || "https://example.test/";
    this.location = null;
    this.characterSet = "UTF-8";
    this.contentType = "text/html";
    this.activeElement = this.body;
    this.__hasFocus = true;
    this.__elementFromPoint = undefined;
  }

  get nodeType() {
    return 9;
  }

  get isConnected() {
    return true;
  }

  get children() {
    return this.__children.slice();
  }

  get childNodes() {
    return this.__children.slice();
  }

  get firstElementChild() {
    return this.documentElement;
  }

  get scrollingElement() {
    return this.documentElement;
  }

  createElement(tag) {
    return new FakeElement(this, tag);
  }

  getRootNode() {
    return this;
  }

  contains(node) {
    return !!node && (node === this || (node.getRootNode && node.getRootNode() === this));
  }

  querySelectorAll(selector) {
    const groups = parseSelectorList(selector);
    return descendants(this).filter((el) => groups.some((parts) => matchComplex(el, parts)));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  getElementById(id) {
    return descendants(this).find((el) => el.getAttribute("id") === String(id)) || null;
  }

  getElementsByTagName(tag) {
    const name = String(tag).toLowerCase();
    return descendants(this).filter((el) => name === "*" || el.localName === name);
  }

  hasFocus() {
    return !!this.__hasFocus;
  }

  setElementFromPoint(value) {
    this.__elementFromPoint = value;
  }

  elementsFromPoint(x, y) {
    return descendants(this)
      .filter((el) => {
        const r = el.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0 && x >= r.left && x < r.right && y >= r.top && y < r.bottom)) {
          return false;
        }
        const style = computedStyleOf(el);
        return style.display !== "none" && style.visibility !== "hidden" && style.pointerEvents !== "none";
      })
      .reverse();
  }

  elementFromPoint(x, y) {
    if (typeof this.__elementFromPoint === "function") {
      return this.__elementFromPoint(x, y);
    }
    if (this.__elementFromPoint !== undefined) {
      return this.__elementFromPoint;
    }
    return this.elementsFromPoint(x, y)[0] || null;
  }

  createTreeWalker(root, whatToShow, filter) {
    return new FakeTreeWalker(root, whatToShow, filter);
  }

  evaluate() {
    throw makeDomError("NotSupportedError", "fake DOM does not implement document.evaluate; assign document.evaluate in the test", 9);
  }
}
Object.assign(FakeDocument, NODE_CONSTANTS);

function createDocument(options = {}) {
  return new FakeDocument(options);
}

/** el(document, tag, attrs?, children?) - children: array of elements, or a text string. */
function el(document, tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    element.setAttribute(name, value);
  }
  if (typeof children === "string") {
    element.textContent = children;
  } else {
    element.append(...children);
  }
  return element;
}

// ─── fake window ──────────────────────────────────────────────────────────

function createMutationObserverClass() {
  class FakeMutationObserver {
    constructor(callback) {
      if (typeof callback !== "function") {
        throw new TypeError("MutationObserver requires a callback");
      }
      this.callback = callback;
      this.targets = [];
      this.records = [];
      this.disconnected = false;
      FakeMutationObserver.instances.push(this);
    }

    observe(target, options = {}) {
      this.disconnected = false;
      this.targets.push({ target, options });
    }

    disconnect() {
      this.targets = [];
      this.records = [];
      this.disconnected = true;
    }

    takeRecords() {
      const records = this.records;
      this.records = [];
      return records;
    }

    get observing() {
      return this.targets.length > 0;
    }

    /** Calls the callback when observing; returns whether it ran. */
    trigger(records) {
      if (!this.targets.length) {
        return false;
      }
      const list = records || [{ type: "childList", target: this.targets[0].target, addedNodes: [], removedNodes: [] }];
      this.callback(list, this);
      return true;
    }

    static triggerAll(records) {
      return FakeMutationObserver.instances.filter((o) => o.observing).filter((o) => o.trigger(records)).length;
    }
  }
  FakeMutationObserver.instances = [];
  return FakeMutationObserver;
}

function createLocation(href) {
  const url = new URL(href);
  const location = {
    assignCalls: [],
    reloadCalls: 0,
    assign(next) {
      location.assignCalls.push(String(next));
    },
    replace(next) {
      location.assignCalls.push(String(next));
    },
    reload() {
      location.reloadCalls += 1;
    },
    toString() {
      return url.href;
    },
  };
  for (const key of ["href", "origin", "protocol", "host", "hostname", "port", "pathname", "search", "hash"]) {
    location[key] = url[key];
  }
  return location;
}

function createWindow(document, overrides = {}) {
  const doc = document || createDocument();
  const win = {};
  installEventTarget(win);
  const innerWidth = overrides.innerWidth || 1280;
  const innerHeight = overrides.innerHeight || 712;
  const location = overrides.location || createLocation(overrides.url || doc.URL || "https://example.test/");
  Object.assign(win, {
    window: win,
    self: win,
    document: doc,
    screenX: 0,
    screenY: 0,
    outerWidth: 1280,
    outerHeight: 800,
    innerWidth,
    innerHeight,
    devicePixelRatio: 1,
    scrollX: 0,
    scrollY: 0,
    scrollCalls: [],
    visualViewport: {
      offsetLeft: 0, offsetTop: 0, pageLeft: 0, pageTop: 0, width: innerWidth, height: innerHeight, scale: 1,
      addEventListener() {}, removeEventListener() {},
    },
    location,
    navigator: { userAgent: "Mozilla/5.0 (FakeDOM) Chrome/130.0.0.0", language: "en-US", languages: ["en-US"], platform: "Win32", webdriver: false },
    performance: { timeOrigin: performance.timeOrigin, now: () => performance.now() },
    getComputedStyle: (element) => computedStyleOf(element),
    MutationObserver: createMutationObserverClass(),
    triggerMutations: (records) => win.MutationObserver.triggerAll(records),
    setTimeout: (...args) => setTimeout(...args),
    clearTimeout: (handle) => clearTimeout(handle),
    setInterval: (...args) => setInterval(...args),
    clearInterval: (handle) => clearInterval(handle),
    requestAnimationFrame: (fn) => setTimeout(() => fn(performance.now()), 16),
    cancelAnimationFrame: (handle) => clearTimeout(handle),
    queueMicrotask,
    WeakRef,
    crypto: { randomUUID: () => nodeCrypto.randomUUID(), getRandomValues: (a) => nodeCrypto.getRandomValues(a) },
    NodeFilter: Object.assign({}, NODE_FILTER),
    Node: Object.assign({}, NODE_CONSTANTS),
    XPathResult: Object.assign({}, XPATH_RESULT),
    Element: FakeElement,
    HTMLElement: FakeElement,
    Document: FakeDocument,
    Event: DomEvent,
    MouseEvent: FakeMouseEvent,
    PointerEvent: FakePointerEvent,
    KeyboardEvent: FakeKeyboardEvent,
    FocusEvent: FakeFocusEvent,
    CustomEvent: FakeCustomEvent,
    console,
    scrollTo(x, y) {
      const target = typeof x === "object" && x !== null ? x : { left: x, top: y };
      win.scrollCalls.push(target);
      if (target.left !== undefined) win.scrollX = Number(target.left) || 0;
      if (target.top !== undefined) win.scrollY = Number(target.top) || 0;
    },
    scrollBy(x, y) {
      const delta = typeof x === "object" && x !== null ? x : { left: x, top: y };
      win.scrollTo({ left: win.scrollX + (Number(delta.left) || 0), top: win.scrollY + (Number(delta.top) || 0) });
    },
  });
  win.scroll = win.scrollTo;
  for (const [alias, key] of [["pageXOffset", "scrollX"], ["pageYOffset", "scrollY"], ["screenLeft", "screenX"], ["screenTop", "screenY"]]) {
    Object.defineProperty(win, alias, { get: () => win[key], enumerable: true, configurable: true });
  }
  const rest = Object.assign({}, overrides);
  delete rest.url;
  Object.assign(win, rest);
  doc.defaultView = win;
  doc.location = win.location;
  doc.URL = win.location.href;
  if (!doc.documentElement.__rectSet) {
    doc.documentElement.setRect({ x: 0, y: 0, width: win.innerWidth, height: win.innerHeight });
    doc.documentElement.scrollWidth = doc.documentElement.scrollWidth || win.innerWidth;
    doc.documentElement.scrollHeight = doc.documentElement.scrollHeight || win.innerHeight;
  }
  return win;
}

// ─── self-test ────────────────────────────────────────────────────────────

async function selfTest() {
  const checks = [];
  const check = (name, fn) => checks.push({ name, fn });

  check("background_state.js defines EmuniumBackground", () => {
    const ctx = loadScripts(["background_state.js"], { chrome: createFakeChrome() });
    try {
      assert.strictEqual(typeof ctx.EmuniumBackground, "object");
      assert.ok(ctx.EmuniumBackground !== null);
      assert.strictEqual(ctx.__harness.loaded.length, 1);
    } finally {
      ctx.__harness.dispose();
    }
  });

  check("content_shared.js defines EmuniumContent", () => {
    const doc = createDocument();
    const ctx = loadScripts(["content_shared.js"], createWindow(doc, { chrome: createFakeChrome() }));
    try {
      assert.strictEqual(typeof ctx.EmuniumContent, "object");
      assert.ok(ctx.EmuniumContent !== null);
      assert.strictEqual(ctx.window, ctx);
    } finally {
      ctx.__harness.dispose();
    }
  });

  check("importScripts loads sibling files into one context", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "emunium-harness-"));
    try {
      fs.writeFileSync(path.join(dir, "a.js"), "globalThis.order = ['a']; importScripts('b.js'); order.push('a-end');");
      fs.writeFileSync(path.join(dir, "b.js"), "order.push('b'); var shared = 42;");
      const ctx = loadScripts(["a.js"], {}, { baseDir: dir });
      assert.deepStrictEqual(Array.from(ctx.order), ["a", "b", "a-end"]);
      assert.strictEqual(ctx.shared, 42);
      loadScripts([path.join(dir, "b.js")], ctx);
      assert.deepStrictEqual(Array.from(ctx.order), ["a", "b", "a-end", "b"]);
      ctx.__harness.evaluate("setInterval(() => {}, 1000); setTimeout(() => {}, 1000);");
      assert.strictEqual(ctx.__harness.timers.intervals.size, 1);
      ctx.__harness.dispose();
      assert.strictEqual(ctx.__harness.timers.timeouts.size, 0);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  check("selectors: tag, #id, .class, [attr], [attr=v], combinators, lists", () => {
    const d = createDocument();
    const form = el(d, "form", { id: "login", class: "card wide" }, [
      el(d, "input", { name: "user", type: "text", "data-testid": "user" }),
      el(d, "button", { type: "submit", class: "primary" }, "Sign in"),
      el(d, "div", { role: "group" }, [el(d, "a", { href: "/x" }, "link")]),
    ]);
    d.body.appendChild(form);
    assert.strictEqual(d.querySelector("#login"), form);
    assert.strictEqual(d.querySelectorAll(".card.wide").length, 1);
    assert.strictEqual(d.querySelector("BUTTON").textContent, "Sign in");
    assert.strictEqual(d.querySelector('[type="submit"]').localName, "button");
    assert.strictEqual(d.querySelector("[type='text']").localName, "input");
    assert.strictEqual(d.querySelector("[data-testid=user]").name, "user");
    assert.strictEqual(d.querySelectorAll("[role]").length, 1);
    assert.strictEqual(d.querySelectorAll("form > a").length, 0);
    assert.strictEqual(d.querySelectorAll("form a").length, 1);
    assert.strictEqual(d.querySelectorAll("input,button,a").length, 3);
    assert.strictEqual(d.querySelectorAll("*").length, 8); // html, head, body + 5
    assert.strictEqual(form.querySelector("a").closest("#login"), form);
    assert.ok(form.matches("form.card"));
    assert.throws(() => d.querySelector("div["), (e) => e.name === "SyntaxError" && e.code === 12);
    assert.throws(() => d.querySelector(""), (e) => e.name === "SyntaxError");
    assert.throws(() => d.querySelector("a:hover"), (e) => e.name === "NotSupportedError");
  });

  check("elements: attributes, tree, text, geometry, styles, value", () => {
    const d = createDocument();
    const win = createWindow(d);
    const input = el(d, "input", { id: "q", value: "hi", disabled: "" });
    const div = el(d, "div", { class: "a b" }, "hello ");
    div.append(el(d, "span", {}, "world"));
    assert.deepStrictEqual(Array.from(input.attributes).map((a) => a.name), ["id", "value", "disabled"]);
    assert.strictEqual(input.value, "hi");
    assert.strictEqual(input.disabled, true);
    input.disabled = false;
    assert.strictEqual(input.hasAttribute("disabled"), false);
    assert.strictEqual(div.value, undefined);
    assert.strictEqual(div.textContent, "hello world");
    div.__innerText = "HELLO";
    assert.strictEqual(div.innerText, "HELLO");
    assert.ok(div.classList.contains("b"));
    assert.strictEqual(div.nodeType, 1);
    assert.strictEqual(div.isConnected, false);
    d.body.append(div, input);
    assert.ok(div.isConnected && d.contains(div) && d.body.contains(div.firstElementChild));
    assert.strictEqual(div.getRootNode(), d);
    div.setRect({ x: 10, y: 20, width: 100, height: 50 });
    assert.deepStrictEqual(
      [div.getBoundingClientRect().right, div.offsetWidth, div.offsetHeight],
      [110, 100, 50]
    );
    div.__computedStyle = { "pointer-events": "none" };
    assert.strictEqual(win.getComputedStyle(div).getPropertyValue("pointer-events"), "none");
    assert.strictEqual(win.getComputedStyle(div).visibility, "visible");
    div.scrollIntoView({ block: "center" });
    assert.deepStrictEqual(div.scrollIntoViewCalls, [{ block: "center" }]);
    div.remove();
    assert.strictEqual(div.isConnected, false);
  });

  check("events: capture, bubble, focus, stopPropagation", () => {
    const d = createDocument();
    const win = createWindow(d);
    const button = el(d, "button");
    d.body.appendChild(button);
    const order = [];
    win.addEventListener("click", () => order.push("win-capture"), true);
    d.body.addEventListener("click", () => order.push("body-bubble"));
    button.addEventListener("click", (e) => order.push("target:" + e.eventPhase));
    win.addEventListener("click", () => order.push("win-bubble"));
    button.click();
    assert.deepStrictEqual(order, ["win-capture", "target:2", "body-bubble", "win-bubble"]);
    order.length = 0;
    d.body.addEventListener("click", (e) => e.stopPropagation(), { once: true });
    button.click();
    assert.deepStrictEqual(order, ["win-capture", "target:2", "body-bubble"]);
    const focused = [];
    d.addEventListener("focusin", (e) => focused.push(e.target));
    button.focus();
    assert.strictEqual(d.activeElement, button);
    assert.deepStrictEqual(focused, [button]);
    const move = new win.PointerEvent("pointermove", { clientX: 5, bubbles: true, isTrusted: true });
    assert.strictEqual(move.isTrusted, true);
    assert.strictEqual(move.clientY, 0);
    assert.strictEqual(button.dispatchEvent(move), true);
  });

  check("elementFromPoint, tree walker, MutationObserver, window", () => {
    const d = createDocument();
    const win = createWindow(d, { screenX: 7 });
    const outer = el(d, "div", { id: "outer" }, [el(d, "p", { id: "inner" }, "text")]);
    d.body.appendChild(outer);
    outer.setRect({ x: 0, y: 0, width: 200, height: 200 });
    outer.firstElementChild.setRect({ x: 10, y: 10, width: 20, height: 20 });
    assert.strictEqual(d.elementFromPoint(15, 15).id, "inner");
    assert.strictEqual(d.elementFromPoint(100, 100).id, "outer");
    d.setElementFromPoint(outer);
    assert.strictEqual(d.elementFromPoint(15, 15), outer);
    d.elementFromPoint = () => null;
    assert.strictEqual(d.elementFromPoint(15, 15), null);
    const walker = d.createTreeWalker(d.body, win.NodeFilter.SHOW_ELEMENT, null);
    const seen = [];
    let node;
    while ((node = walker.nextNode())) seen.push(node.localName);
    assert.deepStrictEqual(seen, ["div", "p"]);
    const rejecting = d.createTreeWalker(d.documentElement, 1, {
      acceptNode: (n) => (n.localName === "body" ? 2 : 1),
    });
    assert.strictEqual(rejecting.nextNode().localName, "head");
    assert.strictEqual(rejecting.nextNode(), null);
    const records = [];
    const observer = new win.MutationObserver((list) => records.push(list));
    observer.observe(d.documentElement, { childList: true, subtree: true });
    assert.strictEqual(win.triggerMutations(), 1);
    observer.disconnect();
    assert.strictEqual(win.triggerMutations(), 0);
    assert.strictEqual(records.length, 1);
    win.scrollTo(0, 300);
    assert.strictEqual(win.pageYOffset, 300);
    assert.strictEqual(win.screenX, 7);
    assert.match(win.crypto.randomUUID(), /^[0-9a-f-]{36}$/);
    assert.strictEqual(typeof win.performance.timeOrigin, "number");
  });

  check("FakeWebSocket lifecycle", () => {
    FakeWebSocket.reset();
    const ws = new FakeWebSocket("ws://127.0.0.1:1234");
    assert.strictEqual(FakeWebSocket.last(), ws);
    assert.throws(() => ws.send("x"), (e) => e.name === "InvalidStateError");
    const got = [];
    ws.onopen = () => got.push("open");
    ws.addEventListener("message", (e) => got.push(JSON.parse(e.data).type));
    ws.onclose = (e) => got.push("close:" + e.code);
    ws.open();
    ws.send(JSON.stringify({ type: "hello", protocol: 2 }));
    ws.serverSend({ type: "welcome", protocol: 2 });
    ws.serverClose(4002, "superseded");
    ws.send("{}");
    assert.deepStrictEqual(ws.sent, [{ type: "hello", protocol: 2 }]);
    assert.deepStrictEqual(ws.droppedSent, [{}]);
    assert.deepStrictEqual(got, ["open", "welcome", "close:4002"]);
    assert.strictEqual(ws.readyState, FakeWebSocket.CLOSED);
    assert.strictEqual(ws.OPEN, 1);
    FakeWebSocket.reset();
  });

  check("fake chrome: events, tabs, overrides, callbacks, fetch", async () => {
    const chrome = createFakeChrome({ files: { "session.json": { protocol: 2, port: 5555, token: "t" } } });
    const seen = [];
    const listener = (tabId) => seen.push(tabId);
    chrome.tabs.onRemoved.addListener(listener);
    chrome.tabs.onRemoved.addListener(listener);
    const tab = await chrome.tabs.create({ url: "https://two.test/" });
    const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    assert.strictEqual(active.id, tab.id);
    assert.strictEqual(await chrome.tabs.getZoom(tab.id), 1);
    await chrome.tabs.remove(tab.id);
    assert.deepStrictEqual(seen, [tab.id]);
    chrome.tabs.onRemoved.removeListener(listener);
    assert.strictEqual(chrome.tabs.onRemoved.hasListeners(), false);
    await assert.rejects(chrome.tabs.sendMessage(1, {}), /Receiving end does not exist/);
    chrome.tabs.sendMessage = spy(async (tabId, msg) => ({ ok: true, result: msg.method }));
    assert.deepStrictEqual(await chrome.tabs.sendMessage(1, { method: "ping" }), { ok: true, result: "ping" });
    assert.strictEqual(chrome.tabs.sendMessage.calls.length, 1);
    const lastError = await new Promise((resolve) => chrome.tabs.get(999, () => resolve(chrome.runtime.lastError)));
    assert.match(lastError.message, /No tab with id: 999/);
    assert.strictEqual(chrome.runtime.lastError, undefined);
    const fetch = createFakeFetch(chrome);
    const response = await fetch(chrome.runtime.getURL("session.json") + "?t=1", { cache: "no-store" });
    assert.deepStrictEqual(await response.json(), { protocol: 2, port: 5555, token: "t" });
    assert.strictEqual((await fetch(chrome.runtime.getURL("missing.json"))).status, 404);
    await assert.rejects(fetch("https://example.com/"), TypeError);
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      setTimeout(() => sendResponse({ echo: msg.n, tab: sender.tab.id }), 1);
      return true;
    });
    assert.deepStrictEqual(await dispatchMessage(chrome.runtime.onMessage, { n: 3 }, { tab: { id: 1 } }), { echo: 3, tab: 1 });
    const results = await chrome.scripting.executeScript({ target: { tabId: 1 }, func: () => 1 });
    assert.strictEqual(results[0].frameId, 0);
  });

  let failed = 0;
  for (const { name, fn } of checks) {
    try {
      await fn();
      console.log(`ok   ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`FAIL ${name}\n     ${(error && error.stack) || error}`);
    }
  }
  console.log(`harness self-test: ${checks.length - failed} passed, ${failed} failed`);
  return failed === 0;
}

module.exports = {
  REPO_ROOT,
  EXTENSION_DIR,
  spy,
  tick,
  makeDomError,
  ChromeEvent,
  dispatchMessage,
  makeApi,
  createFakeChrome,
  createFakeFetch,
  FakeWebSocket,
  loadScripts,
  createDocument,
  createWindow,
  el,
  parseSelectorList,
  matchesSelector,
  DomEvent,
  FakeMouseEvent,
  FakePointerEvent,
  FakeKeyboardEvent,
  FakeFocusEvent,
  FakeCustomEvent,
  FakeElement,
  FakeDocument,
  FakeTreeWalker,
  NODE_FILTER,
  NODE_CONSTANTS,
  XPATH_RESULT,
  selfTest,
};

if (require.main === module) {
  if (process.argv.includes("--self-test")) {
    selfTest().then(
      (ok) => process.exit(ok ? 0 : 1),
      (error) => {
        console.error(error);
        process.exit(1);
      }
    );
  } else {
    console.log("usage: node tests/js/harness.js --self-test");
  }
}
