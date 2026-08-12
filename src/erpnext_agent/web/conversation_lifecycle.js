"use strict";

(function exposeConversationLifecycle(globalObject) {
  function normalizeTitle(value, maxLength = 80) {
    const normalized = String(value ?? "").trim().replace(/\s+/gu, " ");
    if (!normalized) {
      throw new Error("对话标题不能为空");
    }
    if ([...normalized].length > maxLength) {
      throw new Error(`对话标题不能超过 ${maxLength} 个字符`);
    }
    return normalized;
  }

  function replaceConversation(conversations, replacement) {
    return conversations
      .map((conversation) =>
        conversation.conversation_id === replacement.conversation_id
          ? { ...conversation, ...replacement }
          : conversation,
      )
      .sort((left, right) => {
        const leftTime = Date.parse(left.updated_at) || 0;
        const rightTime = Date.parse(right.updated_at) || 0;
        return rightTime - leftTime;
      });
  }

  function removeConversation(conversations, conversationId) {
    return conversations.filter(
      (conversation) => conversation.conversation_id !== conversationId,
    );
  }

  const api = Object.freeze({
    normalizeTitle,
    replaceConversation,
    removeConversation,
  });
  globalObject.ConversationLifecycle = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window === "undefined" ? globalThis : window);
