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
    _patrol = (
        "巡检",
        "异常",
        "预警",
        "逾期",
        "业绩",
        "环比",
        "同比",
        "趋势",
        "建议",
        "threshold",
        "patrol",
    )
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

    def route_with_context(
        self,
        message: str,
        previous_user_messages: list[str],
    ) -> RouteDecision:
        """Inherit the latest explicit intent only for an incomplete follow-up."""

        current = self.route(message)
        if current.intent != Intent.CLARIFY:
            return current
        for previous_message in reversed(previous_user_messages):
            previous = self.route(previous_message)
            if previous.intent != Intent.CLARIFY:
                return RouteDecision(
                    intent=previous.intent,
                    target_agent=previous.target_agent,
                    reason=f"follow-up to previous {previous.intent.value} intent",
                )
        return current
