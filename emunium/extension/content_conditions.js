(() => {
  "use strict";

  const scope = globalThis.EmuniumContent;

  function isVisible(element) {
    if (element.offsetWidth <= 0 && element.offsetHeight <= 0) {
      return false;
    }
    const style = getComputedStyle(element);
    return style.visibility !== "hidden" && parseFloat(style.opacity) > 0;
  }

  function isClickable(element) {
    if (!isVisible(element) || element.disabled) {
      return false;
    }
    return getComputedStyle(element).pointerEvents !== "none";
  }

  function isUnobscured(element) {
    const rect = element.getBoundingClientRect();
    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;
    const topElement = document.elementFromPoint(centerX, centerY);
    return topElement !== null && (topElement === element || element.contains(topElement));
  }

  const ConditionEvaluators = {
    visible: (el) => isVisible(el),
    hidden: (el) => el !== null && !isVisible(el),
    detached: (el) => el === null || !el.isConnected,
    clickable: (el) => isClickable(el),
    unobscured: (el) => isUnobscured(el),
    text_not_empty: (el) =>
      Boolean((el.innerText || el.textContent || "").trim()),
    text_contains: (el, cond) =>
      (el.innerText || el.textContent || "").includes(cond.value),
    has_attribute: (el, cond) =>
      el.hasAttribute(cond.name) &&
      (cond.value == null || el.getAttribute(cond.name) === cond.value),
    without_attribute: (el, cond) => !el.hasAttribute(cond.name),
    has_class: (el, cond) => el.classList.contains(cond.value),
    has_style: (el, cond) =>
      window.getComputedStyle(el).getPropertyValue(cond.name) === cond.value,
    custom_js: (el, cond) => {
      try {
        return Boolean(new Function("el", cond.code)(el));
      } catch {
        return false;
      }
    },
    stable: () => true,
    count_gt: (el, cond, allEls) => allEls.length > cond.value,
    count_eq: (el, cond, allEls) => allEls.length === cond.value,
  };

  function evaluate(el, condition, allElements) {
    const type = condition.type;
    if (type === "any_of") {
      return condition.conditions.some((group) =>
        group.every((c) => evaluate(el, c, allElements))
      );
    }
    if (type === "all_of") {
      return condition.conditions.every((group) =>
        group.every((c) => evaluate(el, c, allElements))
      );
    }
    if (type === "not") {
      return !condition.condition.every((c) => evaluate(el, c, allElements));
    }
    const evaluator = ConditionEvaluators[type];
    return evaluator ? evaluator(el, condition, allElements) : true;
  }

  function matchesCondition(el, condition, allElements) {
    return evaluate(el, condition, allElements);
  }

  function hasDetachedCondition(conditions) {
    if (!Array.isArray(conditions)) {
      return false;
    }
    return conditions.some((c) => {
      if (c.type === "detached") return true;
      if (c.type === "any_of" || c.type === "all_of") {
        return c.conditions.some((group) => hasDetachedCondition(group));
      }
      if (c.type === "not" && Array.isArray(c.condition)) {
        return hasDetachedCondition(c.condition);
      }
      return false;
    });
  }

  function checkElementConditions(element, state, conditions, allElements) {
    const allEls = allElements || [];
    if (element === null) {
      if (!hasDetachedCondition(conditions)) {
        return false;
      }
      return conditions.every((c) => evaluate(null, c, allEls));
    }
    if (state === "visible" && !isVisible(element)) {
      return false;
    }
    if (state === "clickable" && !isClickable(element)) {
      return false;
    }
    if (
      state === "unobscured" &&
      (!isVisible(element) || !isUnobscured(element))
    ) {
      return false;
    }
    if (!Array.isArray(conditions) || conditions.length === 0) {
      return true;
    }
    return conditions.every((c) => evaluate(element, c, allEls));
  }

  Object.assign(scope, {
    checkElementConditions,
    evaluate,
    hasDetachedCondition,
    isClickable,
    isUnobscured,
    isVisible,
    matchesCondition,
  });
})();
