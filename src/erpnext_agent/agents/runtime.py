from __future__ import annotations

import secrets
from dataclasses import dataclass

from agentscope.agent import Agent

from erpnext_agent.actions.proposal import ActionProposalTool
from erpnext_agent.agents.factory import AgentBundle, ConfiguredAgentFactory
from erpnext_agent.agents.orchestrator import Intent
from erpnext_agent.analytics.tool import AnalyticsCalculationTool
from erpnext_agent.mcp.adapter import ERPNextMCPAdapter
from erpnext_agent.mcp.tool_bridge import MCPToolCaller
from erpnext_agent.mcp.toolkit_factory import ERPNextToolkitFactory


class AgentIdentityError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedAgentRuntime:
    bundle: AgentBundle
    erpnext_user: str
    action_proposal_tool: ActionProposalTool | None = None

    def agent_for(self, intent: Intent) -> Agent:
        if intent == Intent.DATA:
            return self.bundle.data_agent
        if intent == Intent.PATROL:
            return self.bundle.patrol_agent
        if intent == Intent.ACTION:
            return self.bundle.action_agent
        return self.bundle.orchestrator


class AgentRuntimeFactory:
    """Build short-lived, user-bound Agent instances for one request."""

    def __init__(
        self,
        *,
        agent_factory: ConfiguredAgentFactory,
        adapter: ERPNextMCPAdapter,
        loop_guard_max_repeats: int = 3,
    ) -> None:
        self._agent_factory = agent_factory
        self._adapter = adapter
        self._loop_guard_max_repeats = loop_guard_max_repeats
        self._toolkit_factory = ERPNextToolkitFactory(adapter)

    async def prepare(
        self,
        *,
        access_token: str,
        expected_user: str,
        tool_caller: MCPToolCaller | None = None,
        action_proposal_tool: ActionProposalTool | None = None,
    ) -> PreparedAgentRuntime:
        specs = await self._adapter.discover_tools(access_token)
        current_user = await self._adapter.current_user(
            access_token,
            discover_first=False,
        )
        if not secrets.compare_digest(current_user.casefold(), expected_user.casefold()):
            raise AgentIdentityError("Agent session and ERPNext MCP identities do not match")

        toolkits = self._toolkit_factory.build_from_specs(
            specs=specs,
            access_token=access_token,
            caller=tool_caller,
            action_proposal_tool=action_proposal_tool,
            # Stateless deterministic calculator; a fresh instance per request
            # mirrors the per-request Agent/Toolkit lifecycle.
            analytics_tool=AnalyticsCalculationTool(),
            max_repeats=self._loop_guard_max_repeats,
        )
        bundle = self._agent_factory.build(
            orchestrator_toolkit=toolkits.orchestrator,
            data_toolkit=toolkits.data,
            action_toolkit=toolkits.action,
            patrol_toolkit=toolkits.patrol,
        )
        return PreparedAgentRuntime(
            bundle=bundle,
            erpnext_user=current_user,
            action_proposal_tool=action_proposal_tool,
        )
