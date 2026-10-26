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

  // Watchdog: connect() itself is single-flight and backs off; this only restarts a
  // stalled loop and never shortens a pending backoff.
  setInterval(() => background.ensureConnected(), 5000);
})();
