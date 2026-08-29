"""AgentScope middleware that turns terminal read results into a final answer."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.message import AssistantMsg, HintBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChoice, ToolChunk, ToolResponse

logger = logging.getLogger(__name__)

HINT_CONVERGE = (
    "终止型只读工具已经成功返回所需数据。现在直接根据该结果回答用户，不得再调用任何工具。"
)
HINT_EMPTY_RETRY = (
    "上一次只读列表查询成功但返回空结果。允许最多再做一次核实性查询：例如把名称过滤改用"
    " like 模糊匹配、核对物料组等名称的准确写法，或改用更合适的领域工具；不得用完全相同"
    "的参数重复调用。核实后仍为空，就如实回答没有查询到符合条件的记录。"
)
HINT_EMPTY_FINAL = (
    "列表查询再次返回空结果。现在必须直接回答用户：如实说明没有查询到符合条件的记录，"
    "并说明已核实的查询条件；不得编造数据，也不得再调用任何工具。"
)


def _result_rows_empty(result: ToolResponse | ToolChunk) -> bool:
    """Return True only when the payload definitively carries an empty rows list.

    Read tools return one chunk whose text block holds the normalized MCP
    envelope ``{"ok": true, "data": {...}}``; ``get_list`` puts the result rows
    under ``data.rows``. Anything unparseable or of an unknown shape is treated
    as non-empty so the middleware falls back to the original converge behavior.
    """
    content = getattr(result, "content", None)
    if not isinstance(content, (list, tuple)):
        return False
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            return not data["rows"]
    return False


class TerminalToolConvergenceMiddleware(MiddlewareBase):
    """Force a text-only reasoning pass after a successful terminal read.

    Some OpenAI-compatible models repeatedly invoke a generic list/count tool
    even after it has returned everything required for the answer. AgentScope
    otherwise keeps accepting those calls until ``max_iters`` is exhausted.
    This middleware records successful calls to explicitly terminal tools
    and makes the following model pass text-only, preserving the tool result
    while guaranteeing convergence.

    Empty list results are the exception: a first empty ``rows`` payload from
    an ``empty_retry_tools`` member grants exactly one corrective retry
    (better filter, verified name, or a domain tool) instead of locking a
    possibly wrong "found nothing" answer in. A second empty result converges
    with an honest-empty instruction. Aggregate domain tools are single-shot
    by design, so their empty result converges immediately.
    """

    def __init__(
        self,
        terminal_tools: frozenset[str],
        *,
        empty_retry_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._terminal_tools = terminal_tools
        self._empty_retry_tools = empty_retry_tools
        # reply_id -> hint text for the forced text-only pass.
        self._converged: dict[str, str] = {}
        # Reply ids that already used their one corrective empty-result retry.
        self._retry_used: set[str] = set()
        # Reply ids whose retry grant has not been announced yet.
        self._pending_retry_hint: set[str] = set()

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        tool_call = input_kwargs["tool_call"]
        succeeded = False
        last_success: ToolResponse | ToolChunk | None = None
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
                last_success = item
            yield item

        if not succeeded or tool_call.name not in self._terminal_tools or last_success is None:
            return
        reply_id = agent.state.reply_id
        if (
            tool_call.name in self._empty_retry_tools
            and _result_rows_empty(last_success)
        ):
            if reply_id in self._retry_used:
                self._converged[reply_id] = HINT_EMPTY_FINAL
                self._pending_retry_hint.discard(reply_id)
                logger.info(
                    "Terminal tool returned an empty list a second time; next reasoning "
                    "must answer honestly agent=%s tool=%s reply_id=%s",
                    agent.name,
                    tool_call.name,
                    reply_id,
                )
            else:
                self._retry_used.add(reply_id)
                self._pending_retry_hint.add(reply_id)
                logger.info(
                    "Terminal tool returned an empty list; granting one corrective retry "
                    "agent=%s tool=%s reply_id=%s",
                    agent.name,
                    tool_call.name,
                    reply_id,
                )
            return
        self._converged[reply_id] = HINT_CONVERGE
        logger.info(
            "Terminal tool result is ready; next reasoning will be text-only "
            "agent=%s tool=%s reply_id=%s",
            agent.name,
            tool_call.name,
            reply_id,
        )

    async def on_reasoning(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        reply_id = agent.state.reply_id
        if reply_id in self._converged:
            logger.info(
                "Forcing text-only terminal reasoning agent=%s reply_id=%s",
                agent.name,
                reply_id,
            )
            self._append_hint(agent, self._converged[reply_id])
            input_kwargs["tool_choice"] = ToolChoice(mode="none")
        elif reply_id in self._pending_retry_hint:
            self._pending_retry_hint.discard(reply_id)
            self._append_hint(agent, HINT_EMPTY_RETRY)

        async for item in next_handler(**input_kwargs):
            yield item

    @staticmethod
    def _append_hint(agent: Agent, text: str) -> None:
        hint = HintBlock(hint=text)
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
                )
            )
