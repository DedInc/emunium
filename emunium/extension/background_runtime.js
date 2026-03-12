(() => {
  "use strict";

  const scope = globalThis.EmuniumBackground;

  function handleExecMessage(msg, sender, sendResponse) {
    chrome.scripting
      .executeScript({
        target: { tabId: sender.tab.id },
        world: "MAIN",
        args: [msg.code],
        func: (code) => {
          try {
            const result = new Function(code)();
            return {
              success: true,
              result: result !== undefined ? String(result) : null,
            };
          } catch (error) {
            return { error: error.message };
          }
        },
      })
      .then((results) => {
        sendResponse(results?.[0]?.result || { error: "No result" });
      })
      .catch((error) => {
        sendResponse({ error: error.message });
      });
    return true;
  }

  function handleReadyMessage(msg, sender) {
    if (sender.frameId === 0) {
      scope.state.readyTabs.add(sender.tab.id);
      if (sender.documentId) {
        scope.state.tabDocIds.set(sender.tab.id, sender.documentId);
      }
    }

    scope.send({
      event: "tabReady",
      tabId: sender.tab.id,
      url: msg.url,
      frameId: sender.frameId,
    });
  }

  function registerRuntimeListeners() {
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      if (msg.__emunium_exec__ && sender.tab) {
        return handleExecMessage(msg, sender, sendResponse);
      }
      if (msg.__emunium_ready__ && sender.tab) {
        handleReadyMessage(msg, sender);
      }
      return undefined;
    });

    chrome.webNavigation.onBeforeNavigate.addListener((details) => {
      if (details.frameId === 0) {
        scope.clearTrackedTab(details.tabId);
      }
    });

    chrome.tabs.onRemoved.addListener((tabId) => {
      scope.clearTrackedTab(tabId);
      scope.clearPinnedTab(tabId);
    });
  }

  Object.assign(scope, { registerRuntimeListeners });
})();
