from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentscope.tool import Toolkit

from erpnext_agent.mcp.adapter import ERPNextMCPAdapter
from erpnext_agent.mcp.policy import (
    ACTION_AGENT_TOOLS,
    DATA_AGENT_TOOLS,
    ORCHESTRATOR_TOOLS,
    PATROL_AGENT_TOOLS,
)
from erpnext_agent.mcp.tool_bridge import ERPNextMCPTool, validate_tool_spec


@dataclass(frozen=True, slots=True)
class AgentToolkits:
    orchestrator: Toolkit
    data: Toolkit
    action: Toolkit
    patrol: Toolkit


class ERPNextToolkitFactory:
    def __init__(self, adapter: ERPNextMCPAdapter) -> None:
        self._adapter = adapter

    async def build(self, *, access_token: str) -> AgentToolkits:
        specs = await self._adapter.discover_tools(access_token)
        return self.build_from_specs(specs=specs, access_token=access_token)

    def build_from_specs(
        self,
        *,
        specs: list[dict[str, Any]],
        access_token: str,
    ) -> AgentToolkits:
        by_name: dict[str, dict[str, Any]] = {}
        for spec in specs:
            name, _ = validate_tool_spec(spec)
            if name in by_name:
                raise ValueError(f"Duplicate MCP tool definition: {name}")
            by_name[name] = spec

        def toolkit(allowed: frozenset[str]) -> Toolkit:
            missing = allowed - by_name.keys()
            if missing:
                raise ValueError(f"MCP contract is missing tools: {sorted(missing)}")
            return Toolkit(
                tools=[
                    ERPNextMCPTool(
                        spec=by_name[name],
                        adapter=self._adapter,
                        access_token=access_token,
                    )
                    for name in sorted(allowed)
                ]
            )

        return AgentToolkits(
            orchestrator=toolkit(ORCHESTRATOR_TOOLS),
            data=toolkit(DATA_AGENT_TOOLS),
            action=toolkit(ACTION_AGENT_TOOLS),
            patrol=toolkit(PATROL_AGENT_TOOLS),
        )
