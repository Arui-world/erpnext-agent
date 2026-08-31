from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.model import ChatModelBase
from agentscope.tool import Toolkit

from erpnext_agent.agents.convergence import TerminalToolConvergenceMiddleware
from erpnext_agent.agents.model_factory import build_chat_model
from erpnext_agent.agents.prompts import (
    ACTION_SYSTEM_PROMPT,
    DATA_SYSTEM_PROMPT,
    MODEL_CHAT_SYSTEM_PROMPT,
    ORCHESTRATOR_SYSTEM_PROMPT,
    PATROL_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
)
from erpnext_agent.agents.time_context import format_business_date_context
from erpnext_agent.config import Settings


@dataclass(frozen=True, slots=True)
class AgentBundle:
    orchestrator: Agent
    data_agent: Agent
    action_agent: Agent
    patrol_agent: Agent


class ConfiguredAgentFactory:
    """Create user-scoped Agent bundles from application Settings.

    The provider model is configuration-scoped, while every call to `build`
    creates fresh Agent instances and therefore fresh conversation state. The
    supplied Toolkits must already be filtered and bound to the current user's
    MCP access token.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @cached_property
    def model(self) -> ChatModelBase:
        return build_chat_model(self._settings)

    def build(
        self,
        *,
        orchestrator_toolkit: Toolkit,
        data_toolkit: Toolkit,
        action_toolkit: Toolkit,
        patrol_toolkit: Toolkit,
    ) -> AgentBundle:
        return build_agent_bundle(
            model=self.model,
            orchestrator_toolkit=orchestrator_toolkit,
            data_toolkit=data_toolkit,
            action_toolkit=action_toolkit,
            patrol_toolkit=patrol_toolkit,
            max_retries=self._settings.model_max_retries,
            patrol_max_iterations=self._settings.patrol_max_iterations,
            # Computed per request: business agents must resolve 本月/上月/
            # 本季度 against the server clock instead of model memory. It lives
            # in the system prompt because an extra leading conversation message
            # measurably made the model answer counts without calling any tool.
            date_context=format_business_date_context(),
        )

    def build_model_chat_agent(self) -> Agent:
        """Create a request-scoped, tool-free agent for model connectivity chat."""

        return Agent(
            name="model_assistant",
            system_prompt=MODEL_CHAT_SYSTEM_PROMPT,
            model=self.model,
            toolkit=Toolkit(),
            model_config=ModelConfig(max_retries=self._settings.model_max_retries),
            react_config=ReActConfig(max_iters=2, stop_on_reject=True),
        )

    def build_summary_agent(self) -> Agent:
        """Create a tool-free agent that only compresses persisted conversation memory."""

        return Agent(
            name="conversation_summarizer",
            system_prompt=SUMMARY_SYSTEM_PROMPT,
            model=self.model,
            toolkit=Toolkit(),
            model_config=ModelConfig(max_retries=self._settings.model_max_retries),
            react_config=ReActConfig(max_iters=2, stop_on_reject=True),
        )


def build_agent_bundle(
    *,
    model: ChatModelBase,
    orchestrator_toolkit: Toolkit,
    data_toolkit: Toolkit,
    action_toolkit: Toolkit,
    patrol_toolkit: Toolkit,
    max_retries: int = 1,
    patrol_max_iterations: int = 8,
    date_context: str | None = None,
) -> AgentBundle:
    """Construct AgentScope 2.0.5 agents from already policy-filtered toolkits.

    Model-provider construction and the custom ToolBase bridge are deliberately separate;
    neither tokens nor MCP clients belong in serializable agent state. ``date_context``
    is appended to the business agents' system prompts so relative time resolves
    against the server clock; the tool-free orchestrator never needs it.
    """

    def make(
        name: str,
        prompt: str,
        toolkit: Toolkit,
        *,
        terminal_tools: frozenset[str] = frozenset(),
        empty_retry_tools: frozenset[str] = frozenset(),
        max_iters: int = 8,
        with_date: bool = False,
    ) -> Agent:
        system_prompt = prompt
        if with_date and date_context:
            header = "运行时日期上下文（服务器时钟，仅供期间换算）："
            system_prompt = f"{prompt}\n\n{header}\n{date_context}"
        return Agent(
            name=name,
            system_prompt=system_prompt,
            model=model,
            toolkit=toolkit,
            middlewares=(
                [
                    TerminalToolConvergenceMiddleware(
                        terminal_tools,
                        empty_retry_tools=empty_retry_tools,
                    )
                ]
                if terminal_tools
                else None
            ),
            model_config=ModelConfig(max_retries=max_retries),
            react_config=ReActConfig(max_iters=max_iters, stop_on_reject=True),
        )

    return AgentBundle(
        orchestrator=make("orchestrator", ORCHESTRATOR_SYSTEM_PROMPT, orchestrator_toolkit),
        data_agent=make(
            "data_agent",
            DATA_SYSTEM_PROMPT,
            data_toolkit,
            terminal_tools=frozenset(
                {
                    "erpnext_get_count",
                    "erpnext_get_list",
                    "erpnext_get_item_group_low_stock",
                }
            ),
            empty_retry_tools=frozenset({"erpnext_get_list"}),
            with_date=True,
        ),
        action_agent=make(
            "action_agent",
            ACTION_SYSTEM_PROMPT,
            action_toolkit,
            with_date=True,
        ),
        patrol_agent=make(
            "patrol_agent",
            PATROL_SYSTEM_PROMPT,
            patrol_toolkit,
            max_iters=patrol_max_iterations,
            with_date=True,
        ),
    )
