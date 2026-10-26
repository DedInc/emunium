// background_runtime.js - browser event listeners: content readiness, tab lifecycle, wake-ups.
(() => {
  "use strict";

  const scope = globalThis.EmuniumBackground;
  const RECONNECT_ALARM = "emunium-reconnect";

  function handleRuntimeMessage(msg, sender) {
    if (!msg || typeof msg !== "object" || msg.__emunium_ready__ !== 2) {
      return undefined;
    }
    const tab = sender && sender.tab;
    if (!tab || !Number.isInteger(tab.id) || sender.frameId !== 0) {
      return undefined; // readiness is tracked for the top frame only
    }
    scope.markContentReady(tab.id, sender.documentId);
    scope.sendEvent("tabReady", {
      tabId: tab.id,
      url: typeof msg.url === "string" ? msg.url : tab.url || null,
    });
    return undefined;
  }

  function handleTopFrameCommitted(details) {
    if (details && details.frameId === 0) {
      scope.onTopFrameCommitted(details.tabId, details.documentId);
      // Attach mode: the hub's start URL carries a single-use claim nonce.
      scope.handleClaimNavigation(details).catch(() => {});
    }
  }

  function handleTabRemoved(tabId) {
    scope.clearTrackedTab(tabId);
    scope.sendEvent("tabRemoved", { tabId });
  }

  function registerRuntimeListeners() {
    if (scope.state.listenersRegistered) {
      return;
    }
    scope.state.listenersRegistered = true;
    chrome.runtime.onMessage.addListener(handleRuntimeMessage);
    if (chrome.webNavigation && chrome.webNavigation.onCommitted) {
      chrome.webNavigation.onCommitted.addListener(handleTopFrameCommitted);
    }
    chrome.tabs.onRemoved.addListener(handleTabRemoved);
    const wake = () => scope.ensureConnected();
    if (chrome.runtime.onStartup) {
      chrome.runtime.onStartup.addListener(wake);
    }
    if (chrome.runtime.onInstalled) {
      chrome.runtime.onInstalled.addListener(wake);
    }
    registerReconnectAlarm();
  }

  // Timers die with a suspended worker; an alarm wakes it up again to reconnect.
  // The `alarms` permission is granted by the manifest slice, hence the guard.
  function registerReconnectAlarm() {
    if (!chrome.alarms || !chrome.alarms.onAlarm) {
      return;
    }
    chrome.alarms.onAlarm.addListener(handleAlarm);
    try {
      const created = chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 0.5 });
      if (created && typeof created.catch === "function") {
        created.catch(() => {});
      }
    } catch {
      // without the permission the worker still reconnects through its own timers
    }
  }

  function handleAlarm(alarm) {
    if (alarm && alarm.name === RECONNECT_ALARM && !scope.isConnected()) {
      scope.connect();
    }
  }

  Object.assign(scope, {
    registerRuntimeListeners,
    handleRuntimeMessage,
    handleTabRemoved,
  });
})();
