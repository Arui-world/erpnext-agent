"use strict";

(function initializeActionRestore(globalScope) {
  function attachActionsToMessages(messages = [], actions = []) {
    const remaining = new Map();
    for (const action of actions) {
      if (action && typeof action.action_id === "string" && action.action_id) {
        remaining.set(action.action_id, action);
      }
    }

    const restoredMessages = messages.map((message) => {
      if (message?.role !== "assistant" || typeof message.content !== "string") {
        return { ...message, action: null };
      }
      for (const [actionId, action] of remaining) {
        if (message.content.includes(actionId)) {
          remaining.delete(actionId);
          return { ...message, action };
        }
      }
      return { ...message, action: null };
    });

    return {
      messages: restoredMessages,
      unmatchedActions: [...remaining.values()],
    };
  }

  const api = Object.freeze({ attachActionsToMessages });
  globalScope.ActionRestore = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
