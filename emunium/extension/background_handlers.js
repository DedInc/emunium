(() => {
  "use strict";

  const scope = globalThis.EmuniumBackground;

  function sendError(id, error) {
    scope.send({ id, result: { error } });
  }

  function waitForTabLoad(tabId, timeout) {
    return new Promise((resolve) => {
      function listener(updatedTabId, changeInfo) {
        if (updatedTabId === tabId && changeInfo.status === "complete") {
          cleanup();
          resolve();
        }
      }

      function cleanup() {
        chrome.tabs.onUpdated.removeListener(listener);
        clearTimeout(timeoutId);
      }

      chrome.tabs.onUpdated.addListener(listener);
      const timeoutId = setTimeout(() => {
        cleanup();
        resolve();
      }, timeout);
    });
  }

  async function handleNavigate(msg) {
    const tabId = await scope.resolveTabId(msg);
    if (!tabId) {
      sendError(msg.id, "No active tab");
      return;
    }

    scope.state.pinnedTabId = tabId;
    try {
      scope.clearTrackedTab(tabId);
      await chrome.tabs.update(tabId, { url: msg.params.url });
      await waitForTabLoad(tabId, msg.params.timeout || 30000);
      await scope.waitForContentReady(tabId, 5000);
      const tab = await chrome.tabs.get(tabId);
      scope.send({
        id: msg.id,
        result: { success: true, url: tab.url, title: tab.title, tabId },
      });
    } catch (error) {
      sendError(msg.id, error.message);
    }
  }

  async function handleGetTabInfo(msg) {
    const tabId = await scope.resolveTabId(msg);
    if (!tabId) {
      sendError(msg.id, "No active tab");
      return;
    }
    const tab = await chrome.tabs.get(tabId);
    scope.send({
      id: msg.id,
      result: { tabId: tab.id, url: tab.url, title: tab.title, status: tab.status },
    });
  }

  async function handleCreateTab(msg) {
    try {
      const tab = await chrome.tabs.create({ url: msg.params?.url || "about:blank" });
      scope.send({ id: msg.id, result: { tabId: tab.id, url: tab.url } });
    } catch (error) {
      sendError(msg.id, error.message);
    }
  }

  async function handleExecuteScript(msg) {
    const tabId = await scope.resolveTabId(msg);
    if (!tabId) {
      sendError(msg.id, "No active tab");
      return;
    }

    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        args: [msg.params.code],
        func: (code) =>
          new Promise((resolve) => {
            const script = document.createElement("script");
            const callbackName =
              "__emunium_cb_" +
              Date.now() +
              "_" +
              Math.random().toString(36).slice(2);
            window[callbackName] = (value) => {
              delete window[callbackName];
              resolve(value);
            };
            script.textContent = `
              try {
                const __result = (() => { ${code} })();
                window["${callbackName}"]({ success: true, result: __result !== undefined ? String(__result) : null });
              } catch(e) {
                window["${callbackName}"]({ error: e.message });
              }
            `;
            document.documentElement.appendChild(script);
            script.remove();
          }),
      });
      scope.send({ id: msg.id, result: results?.[0]?.result || { error: "No result" } });
    } catch (error) {
      sendError(msg.id, error.message);
    }
  }

  async function handleCloseTab(msg) {
    const tabId = await scope.resolveTabId(msg);
    if (!tabId) {
      sendError(msg.id, "No active tab");
      return;
    }

    try {
      await chrome.tabs.remove(tabId);
      scope.clearPinnedTab(tabId);
      scope.send({ id: msg.id, result: { success: true } });
    } catch (error) {
      sendError(msg.id, error.message);
    }
  }

  async function handleWaitForResponse(msg) {
    const pattern = msg.params?.pattern;
    const timeout = msg.params?.timeout || 10000;
    if (!pattern) {
      sendError(msg.id, "Missing 'pattern' parameter");
      return;
    }
    try {
      const result = await scope.waitForResponse(pattern, timeout);
      if (result) {
        scope.send({ id: msg.id, result });
      } else {
        scope.send({
          id: msg.id,
          result: { error: "Timeout", pattern },
        });
      }
    } catch (error) {
      sendError(msg.id, error.message);
    }
  }

  async function routeBridgeMessage(msg) {
    if (msg.method === "navigate") {
      await handleNavigate(msg);
      return;
    }
    if (msg.method === "getTabInfo") {
      await handleGetTabInfo(msg);
      return;
    }
    if (msg.method === "createTab") {
      await handleCreateTab(msg);
      return;
    }
    if (msg.method === "closeTab") {
      await handleCloseTab(msg);
      return;
    }
    if (msg.method === "executeScript") {
      await handleExecuteScript(msg);
      return;
    }
    if (msg.method === "waitForResponse") {
      await handleWaitForResponse(msg);
      return;
    }
    if (msg.method === "getRecentResponses") {
      scope.send({
        id: msg.id,
        result: { responses: scope.getRecentResponses() },
      });
      return;
    }

    const tabId = msg.tabId || scope.state.pinnedTabId || (await scope.getActiveTabId());
    if (!tabId) {
      sendError(msg.id, "No active tab");
      return;
    }

    const frameId = msg.frameId !== undefined ? msg.frameId : 0;
    const response = await scope.sendToContentScript(tabId, msg, frameId);
    scope.send(response);
  }

  function attachSocketHandlers(socket) {
    socket.onopen = () => {
      console.log("[emunium] Connected to bridge:", scope.state.wsUrl);
      scope.clearReconnect();
      scope.startKeepAlive(socket);
    };

    socket.onmessage = async (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      await routeBridgeMessage(msg);
    };

    socket.onerror = () => {};
    socket.onclose = () => {
      console.log("[emunium] Disconnected from bridge");
      scope.clearKeepAlive(socket);
      if (scope.state.ws === socket) {
        scope.state.ws = null;
      }
      scope.scheduleReconnect(2000);
    };
  }

  Object.assign(scope, {
    attachSocketHandlers,
    routeBridgeMessage,
  });
})();
