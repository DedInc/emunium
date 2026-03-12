importScripts(
  "background_state.js",
  "background_network.js",
  "background_handlers.js",
  "background_runtime.js"
);

(() => {
  "use strict";

  const background = globalThis.EmuniumBackground;
  background.registerRuntimeListeners();
  background.connect();

  setInterval(() => {
    if (!background.state.ws || background.state.ws.readyState !== WebSocket.OPEN) {
      background.connect();
    }
  }, 5000);
})();
