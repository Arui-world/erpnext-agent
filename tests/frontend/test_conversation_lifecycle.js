"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  normalizeTitle,
  removeConversation,
  replaceConversation,
} = require("../../src/erpnext_agent/web/conversation_lifecycle.js");

test("normalizes a user supplied conversation title", () => {
  assert.equal(normalizeTitle("  月度\n库存   分析  "), "月度 库存 分析");
  assert.throws(() => normalizeTitle(" \n "), /不能为空/u);
  assert.throws(() => normalizeTitle("甲".repeat(81)), /不能超过 80/u);
});

test("replaces and reorders a renamed conversation", () => {
  const conversations = [
    { conversation_id: "older", title: "旧", updated_at: "2026-08-10T00:00:00Z" },
    { conversation_id: "newer", title: "新", updated_at: "2026-08-11T00:00:00Z" },
  ];

  const updated = replaceConversation(conversations, {
    conversation_id: "older",
    title: "已重命名",
    updated_at: "2026-08-12T00:00:00Z",
  });

  assert.deepEqual(updated.map((item) => item.conversation_id), ["older", "newer"]);
  assert.equal(updated[0].title, "已重命名");
  assert.equal(conversations[0].title, "旧");
});

test("removes only the selected conversation", () => {
  const conversations = [
    { conversation_id: "keep" },
    { conversation_id: "remove" },
  ];

  assert.deepEqual(removeConversation(conversations, "remove"), [
    { conversation_id: "keep" },
  ]);
});
