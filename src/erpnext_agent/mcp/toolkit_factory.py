from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentscope.tool import ToolBase, Toolkit

from erpnext_agent.mcp.adapter import ERPNextMCPAdapter
from erpnext_agent.mcp.loop_guard import LoopGuardTool
from erpnext_agent.mcp.policy import (
    ACTION_AGENT_TOOLS,
    DATA_AGENT_TOOLS,
    ORCHESTRATOR_TOOLS,
    PATROL_AGENT_TOOLS,
    WRITE_TOOLS,
)
from erpnext_agent.mcp.tool_bridge import ERPNextMCPTool, MCPToolCaller, validate_tool_spec


@dataclass(frozen=True, slots=True)
class AgentToolkits:
    orchestrator: Toolkit
    data: Toolkit
    action: Toolkit
    patrol: Toolkit


class ERPNextToolkitFactory:
    def __init__(self, adapter: ERPNextMCPAdapter) -> None:
        self._adapter = adapter

    async def build(self, *, access_token: str, max_repeats: int = 3) -> AgentToolkits:
        specs = await self._adapter.discover_tools(access_token)
        return self.build_from_specs(
            specs=specs, access_token=access_token, max_repeats=max_repeats
        )

    def build_from_specs(
        self,
        *,
        specs: list[dict[str, Any]],
        access_token: str,
        caller: MCPToolCaller | None = None,
        action_proposal_tool: ToolBase | None = None,
        max_repeats: int = 3,
    ) -> AgentToolkits:
        by_name: dict[str, dict[str, Any]] = {}
        for spec in specs:
            name, _ = validate_tool_spec(spec)
            if name in by_name:
                raise ValueError(f"Duplicate MCP tool definition: {name}")
            by_name[name] = spec

        def toolkit(
            allowed: frozenset[str],
            *,
            extra_tools: list[ToolBase] | None = None,
        ) -> Toolkit:
            missing = allowed - by_name.keys()
            if missing:
                raise ValueError(f"MCP contract is missing tools: {sorted(missing)}")
            tools: list[ToolBase] = []
            for name in sorted(allowed):
                tool: ToolBase = ERPNextMCPTool(
                    spec=by_name[name],
                    adapter=caller or self._adapter,
                    access_token=access_token,
                )
                if tool.is_read_only:
                    tool = LoopGuardTool(tool=tool, max_repeats=max_repeats)
                tools.append(tool)
            for extra in extra_tools or []:
                if extra.name in by_name or any(tool.name == extra.name for tool in tools):
                    raise ValueError(f"Duplicate Action tool definition: {extra.name}")
                tools.append(extra)
            return Toolkit(
                tools=tools,
            )

        action_tools = (
            ACTION_AGENT_TOOLS - WRITE_TOOLS
            if action_proposal_tool is not None
            else ACTION_AGENT_TOOLS
        )
        return AgentToolkits(
            orchestrator=toolkit(ORCHESTRATOR_TOOLS),
            data=toolkit(DATA_AGENT_TOOLS),
            action=toolkit(
                action_tools,
                extra_tools=[action_proposal_tool] if action_proposal_tool is not None else None,
            ),
            patrol=toolkit(PATROL_AGENT_TOOLS),
        )
