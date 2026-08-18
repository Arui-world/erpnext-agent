"""AgentScope middleware that turns terminal read results into a final answer."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.message import AssistantMsg, HintBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChoice, ToolChunk, ToolResponse

logger = logging.getLogger(__name__)


class TerminalToolConvergenceMiddleware(MiddlewareBase):
    """Force a text-only reasoning pass after a successful terminal read.

    Some OpenAI-compatible models repeatedly invoke a generic list/count tool even
    after it has returned everything required for the answer. AgentScope otherwise
    keeps accepting those calls until ``max_iters`` is exhausted. This middleware
    records successful calls to explicitly terminal tools and makes the following
    model pass text-only, preserving the tool result while guaranteeing convergence.
    """

    def __init__(self, terminal_tools: frozenset[str]) -> None:
        self._terminal_tools = terminal_tools
        self._ready_reply_ids: set[str] = set()

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        tool_call = input_kwargs["tool_call"]
        succeeded = False
        async for item in next_handler(**input_kwargs):
            if (
                isinstance(item, ToolResponse)
                and item.state == ToolResultState.SUCCESS
            ) or (
                isinstance(item, ToolChunk)
                and item.is_last
                and item.state == ToolResultState.SUCCESS
            ):
                succeeded = True
            yield item

        if succeeded and tool_call.name in self._terminal_tools:
            self._ready_reply_ids.add(agent.state.reply_id)
            logger.info(
                "Terminal tool result is ready; next reasoning will be text-only "
                "agent=%s tool=%s reply_id=%s",
                agent.name,
                tool_call.name,
                agent.state.reply_id,
            )

    async def on_reasoning(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        if agent.state.reply_id in self._ready_reply_ids:
            logger.info(
                "Forcing text-only terminal reasoning agent=%s reply_id=%s",
                agent.name,
                agent.state.reply_id,
            )
            hint = HintBlock(
                hint=(
                    "终止型只读工具已经成功返回所需数据。现在直接根据该结果回答用户，"
                    "不得再调用任何工具。"
                ),
            )
            if (
                agent.state.context
                and agent.state.context[-1].role == "assistant"
                and agent.state.context[-1].name == agent.name
            ):
                agent.state.context[-1].content.append(hint)
            else:
                agent.state.context.append(
                    AssistantMsg(
                        id=agent.state.reply_id,
                        name=agent.name,
                        content=[hint],
                    ),
                )
            input_kwargs["tool_choice"] = ToolChoice(mode="none")

        async for item in next_handler(**input_kwargs):
            yield item
