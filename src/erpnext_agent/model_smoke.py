from __future__ import annotations

import asyncio

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.message import UserMsg
from agentscope.tool import Toolkit

from erpnext_agent.agents.model_factory import build_chat_model
from erpnext_agent.agents.replies import assistant_text
from erpnext_agent.config import get_settings


async def run_smoke() -> str:
    settings = get_settings()
    agent = Agent(
        name="model_smoke",
        system_prompt=(
            "你是模型连通性检查助手。不要调用工具，只用一句简短中文回答用户。"
        ),
        model=build_chat_model(settings),
        toolkit=Toolkit(),
        model_config=ModelConfig(max_retries=settings.model_max_retries),
        react_config=ReActConfig(max_iters=2, stop_on_reject=True),
    )
    reply = await agent.reply(
        UserMsg(name="user", content="请回复：千问模型连接成功。"),
    )
    text = assistant_text(reply)
    if not text:
        raise RuntimeError("Model returned no text")
    return text


def main() -> None:
    print(asyncio.run(run_smoke()))  # noqa: T201 - explicit operator-facing smoke output


if __name__ == "__main__":
    main()
