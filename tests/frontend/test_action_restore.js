"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { attachActionsToMessages } = require("../../src/erpnext_agent/web/action_restore.js");

test("restores an action onto its persisted assistant message", () => {
  const action = { action_id: "action-123", status: "PENDING" };
  const result = attachActionsToMessages(
    [
      { role: "user", content: "创建草稿" },
      { role: "assistant", content: "待审批 Action：action-123" },
    ],
    [action],
  );

  assert.equal(result.messages[0].action, null);
  assert.deepEqual(result.messages[1].action, action);
  assert.deepEqual(result.unmatchedActions, []);
});

test("keeps actions whose reply was not persisted for a synthetic card", () => {
  const unmatched = { action_id: "action-after-disconnect", status: "APPROVED" };
  const invalid = { status: "PENDING" };
  const result = attachActionsToMessages(
    [{ role: "assistant", content: "流式连接提前断开" }],
    [unmatched, invalid, null],
  );

  assert.equal(result.messages[0].action, null);
  assert.deepEqual(result.unmatchedActions, [unmatched]);
});

test("attaches each action at most once", () => {
  const action = { action_id: "action-once", status: "SUCCEEDED" };
  const result = attachActionsToMessages(
    [
      { role: "assistant", content: "action-once" },
      { role: "assistant", content: "duplicate action-once" },
    ],
    [action],
  );

  assert.deepEqual(result.messages.map((message) => message.action), [action, null]);
});
