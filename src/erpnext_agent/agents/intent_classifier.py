from __future__ import annotations

import asyncio
from typing import Literal

from agentscope.message import Msg, TextBlock, UserMsg
from agentscope.model import ChatModelBase
from pydantic import BaseModel, ConfigDict, Field

from erpnext_agent.agents.orchestrator import Intent, IntentGate, RouteDecision


class IntentClassification(BaseModel):
    """The deliberately small, non-authoritative LLM routing result."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["data", "action", "patrol", "clarify"]
    reason: str = Field(default="", max_length=240)


INTENT_CLASSIFIER_SYSTEM_PROMPT = """
你是 ERPNext Agent 的意图分类器，只返回结构化分类，不回答用户问题，也不调用工具。

将当前请求分类为以下之一：
- data：用户想了解、查找、汇总、比较或分析 ERPNext 中已有的信息；
- action：用户想创建、修改或准备一份可审批的业务草稿；
- patrol：用户想做有范围的巡检、异常检查或风险汇总；
- clarify：请求没有足够业务意图，或只是寒暄、无关问题、无法安全判断。

理解自然语言、口语、同义表达和上下文，不要求用户使用固定词汇。不要把“提交、作废、删除、
过账”这类写操作安全词改判为可执行意图；这些请求由确定性安全策略处理，你这里只能在其余请求
中选择分类。只依据用户消息和提供的历史用户消息分类，不执行任何业务动作。
""".strip()


class IntentClassifier:
    """Use a bounded structured model call, with the legacy gate as fail-safe."""

    def __init__(
        self,
        model: ChatModelBase,
        *,
        timeout_seconds: float = 8.0,
        gate: IntentGate | None = None,
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._gate = gate or IntentGate()

    async def classify(
        self,
        message: str,
        previous_user_messages: list[str] | None = None,
    ) -> RouteDecision:
        """Classify a non-denied message, degrading to the old gate on any failure."""

        previous = previous_user_messages or []
        deterministic = self._gate.route_with_context(message, previous)
        if not message.strip():
            return deterministic
        if deterministic.intent == Intent.DENY:
            return deterministic

        try:
            result = await asyncio.wait_for(
                self._model.generate_structured_output(
                    self._messages(message, previous),
                    IntentClassification,
                ),
                timeout=self._timeout_seconds,
            )
            parsed = IntentClassification.model_validate(result.content)
        except Exception:
            return deterministic

        intent = Intent(parsed.intent)
        if intent == Intent.CLARIFY and deterministic.intent != Intent.CLARIFY:
            return deterministic
        target_agent = {
            Intent.DATA: "data_agent",
            Intent.ACTION: "action_agent",
            Intent.PATROL: "patrol_agent",
        }.get(intent)
        return RouteDecision(
            intent=intent,
            target_agent=target_agent,
            reason=parsed.reason.strip() or "LLM intent classification",
        )

    @staticmethod
    def _messages(message: str, previous_user_messages: list[str]) -> list[Msg]:
        context = ""
        if previous_user_messages:
            recent = [item[-1200:] for item in previous_user_messages[-6:]]
            context = "\n此前用户消息（仅作上下文）：\n" + "\n".join(
                f"- {item}" for item in recent
            )
        return [
            Msg(
                name="intent_classifier",
                role="system",
                content=[TextBlock(text=INTENT_CLASSIFIER_SYSTEM_PROMPT)],
            ),
            UserMsg(name="user", content=f"当前用户消息：\n{message}{context}"),
        ]
