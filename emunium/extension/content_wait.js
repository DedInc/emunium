(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;

  function buildFinder(selector, type) {
    if (type === "xpath") {
      return () =>
        document.evaluate(
          selector,
          document,
          null,
          XPathResult.FIRST_ORDERED_NODE_TYPE,
          null
        ).singleNodeValue;
    }
    if (type === "text") {
      return () => scope.findElementsByText(selector)[0] || null;
    }
    return () => document.querySelector(selector);
  }

  function buildAllFinder(selector, type) {
    if (type === "xpath") {
      return () => {
        const result = document.evaluate(
          selector,
          document,
          null,
          XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
          null
        );
        const nodes = [];
        for (let i = 0; i < result.snapshotLength; i += 1) {
          nodes.push(result.snapshotItem(i));
        }
        return nodes;
      };
    }
    if (type === "text") {
      return () => scope.findElementsByText(selector);
    }
    return () => Array.from(document.querySelectorAll(selector));
  }

  function hasDetachedCondition(conditions) {
    if (!Array.isArray(conditions)) return false;
    return conditions.some((c) => {
      if (c.type === "detached") return true;
      if (c.type === "any_of" || c.type === "all_of") {
        return c.conditions.some((group) => hasDetachedCondition(group));
      }
      if (c.type === "not") return hasDetachedCondition(c.condition);
      return false;
    });
  }

  function needsStableState(state, conditions) {
    return (
      state === "stable" || conditions?.some((c) => c.type === "stable")
    );
  }

  function getStableDuration(conditions) {
    const stable = conditions?.find(
      (c) => c.type === "stable" && c.duration
    );
    return stable ? stable.duration : 300;
  }

  function fingerprint(element) {
    const rect = element.getBoundingClientRect();
    return JSON.stringify({
      x: rect.x,
      y: rect.y,
      w: rect.width,
      h: rect.height,
      text: scope.getElementText(element, 200),
      href: element.href || null,
      value: element.value !== undefined ? element.value : null,
      connected: element.isConnected,
      ready: document.readyState,
    });
  }

  function createWaitTracker(resolve, findElement, findAllElements, options) {
    let lastFingerprint = null;
    let stableSince = 0;
    let resolved = false;

    function cleanup(observer, intervalId, timeoutId) {
      if (observer) observer.disconnect();
      if (intervalId) clearInterval(intervalId);
      if (timeoutId) clearTimeout(timeoutId);
    }

    function resolveDetached(observer, intervalId, timeoutId) {
      resolved = true;
      cleanup(observer, intervalId, timeoutId);
      resolve({ detached: true, selector: options.selector });
    }

    function tryDetached(observer, intervalId, timeoutId) {
      if (!hasDetachedCondition(options.conditions)) return false;
      const ok = scope.checkElementConditions(
        null, options.state, options.conditions, []
      );
      if (ok) {
        resolveDetached(observer, intervalId, timeoutId);
        return true;
      }
      return false;
    }

    function maybeResolve(observer, intervalId, timeoutId) {
      if (resolved) return;
      const element = findElement();
      if (!element) {
        tryDetached(observer, intervalId, timeoutId);
        return;
      }
      const allEls = findAllElements();
      if (!scope.checkElementConditions(
        element, options.state, options.conditions, allEls
      )) {
        return;
      }
      if (options.needsStable) {
        const fp = fingerprint(element);
        if (fp !== lastFingerprint) {
          lastFingerprint = fp;
          stableSince = Date.now();
          return;
        }
        if (Date.now() - stableSince < options.stableDuration) return;
      }
      resolved = true;
      cleanup(observer, intervalId, timeoutId);
      resolve(scope.serializeElement(element));
    }

    function seed() {
      const element = findElement();
      if (!element) {
        if (
          hasDetachedCondition(options.conditions) &&
          scope.checkElementConditions(null, options.state, options.conditions, [])
        ) {
          resolved = true;
          resolve({ detached: true, selector: options.selector });
          return true;
        }
        return false;
      }
      const allEls = findAllElements();
      if (!scope.checkElementConditions(
        element, options.state, options.conditions, allEls
      )) {
        return false;
      }
      if (!options.needsStable) {
        resolved = true;
        resolve(scope.serializeElement(element));
        return true;
      }
      lastFingerprint = fingerprint(element);
      stableSince = Date.now();
      return false;
    }

    function finish(observer, intervalId) {
      if (resolved) return;
      resolved = true;
      cleanup(observer, intervalId, null);
      const element = findElement();
      if (!element) {
        if (
          hasDetachedCondition(options.conditions) &&
          scope.checkElementConditions(null, options.state, options.conditions, [])
        ) {
          resolve({ detached: true, selector: options.selector });
          return;
        }
      } else {
        const allEls = findAllElements();
        if (
          scope.checkElementConditions(
            element, options.state, options.conditions, allEls
          ) &&
          !options.needsStable
        ) {
          resolve(scope.serializeElement(element));
          return;
        }
      }
      resolve({
        error: "Timeout",
        selector: options.selector,
        url: location.href,
        title: document.title,
      });
    }

    return { finish, maybeResolve, seed };
  }

  function waitForSelector({ selector, type, timeout, state, conditions }) {
    const options = {
      conditions,
      needsStable: needsStableState(state, conditions),
      selector,
      stableDuration: getStableDuration(conditions),
      state,
    };
    const selectorType = type || "css";
    const findElement = buildFinder(selector, selectorType);
    const findAllElements = buildAllFinder(selector, selectorType);
    const timeoutMs = timeout || 10000;

    return new Promise((resolve) => {
      const tracker = createWaitTracker(
        resolve, findElement, findAllElements, options
      );
      if (tracker.seed()) return;

      let observer;
      let intervalId;
      let timeoutId;
      observer = new MutationObserver(() => {
        tracker.maybeResolve(observer, intervalId, timeoutId);
      });
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
      });
      intervalId = setInterval(() => {
        tracker.maybeResolve(observer, intervalId, timeoutId);
      }, 50);
      timeoutId = setTimeout(() => {
        tracker.finish(observer, intervalId);
      }, timeoutMs);
    });
  }

  Object.assign(scope, { waitForSelector });
})();
