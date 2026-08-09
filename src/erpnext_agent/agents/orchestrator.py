from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Intent(StrEnum):
    DATA = "data"
    ACTION = "action"
    PATROL = "patrol"
    DENY = "deny"
    CLARIFY = "clarify"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    intent: Intent
    target_agent: str | None
    reason: str


class IntentGate:
    """A conservative pre-model gate; authorization remains code-enforced downstream."""

    _forbidden = ("提交", "作废", "删除", "过账", "submit", "cancel", "delete")
    _action = ("创建", "新建", "修改", "更新", "create", "update")
    _patrol = ("巡检", "异常", "预警", "逾期", "threshold", "patrol")
    _data = ("查询", "查看", "多少", "统计", "分析", "库存", "应收", "list", "show")

    def route(self, message: str) -> RouteDecision:
        normalized = message.strip().lower()
        if not normalized:
            return RouteDecision(Intent.CLARIFY, None, "message is empty")
        if any(word in normalized for word in self._forbidden):
            return RouteDecision(Intent.DENY, None, "requested operation is outside MVP scope")
        if any(word in normalized for word in self._action):
            return RouteDecision(Intent.ACTION, "action_agent", "draft write intent detected")
        if any(word in normalized for word in self._patrol):
            return RouteDecision(Intent.PATROL, "patrol_agent", "patrol intent detected")
        if any(word in normalized for word in self._data):
            return RouteDecision(Intent.DATA, "data_agent", "read-only intent detected")
        return RouteDecision(Intent.CLARIFY, None, "intent is incomplete")

