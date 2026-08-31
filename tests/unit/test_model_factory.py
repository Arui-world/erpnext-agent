from collections.abc import Iterator

import pytest
from agentscope.model import DashScopeChatModel, OpenAIChatModel
from agentscope.tool import Toolkit
from pydantic import SecretStr

from erpnext_agent.agents.factory import ConfiguredAgentFactory
from erpnext_agent.agents.model_factory import ModelConfigurationError, build_chat_model
from erpnext_agent.config import Settings


@pytest.fixture(autouse=True)
def model_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "MODEL_PROVIDER",
        "MODEL_NAME",
        "MODEL_API_KEY",
        "MODEL_BASE_URL",
        "MODEL_ENABLE_THINKING",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def settings_from_environment(monkeypatch: pytest.MonkeyPatch, **model_env: str) -> Settings:
    for name, value in model_env.items():
        monkeypatch.setenv(name, value)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]  # BaseSettings runtime option
        database_url="postgresql+asyncpg://agent:password@postgres/agent",
        redis_url=SecretStr("redis://:password@redis/0"),
        erpnext_base_url="http://erpnext:8000",
        erpnext_site="dev.localhost",
        oauth_client_id="client-id",
        oauth_client_secret=SecretStr("client-secret"),  # noqa: S106
        oauth_redirect_uri="http://localhost:8001/api/v1/auth/callback",
        session_secret=SecretStr(  # noqa: S106 - inert test fixture
            "a-session-secret-with-at-least-32-characters",
        ),
        token_encryption_key=SecretStr(  # noqa: S106 - deterministic test key
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ),
    )


def test_dashscope_model_is_created_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="dashscope",
        MODEL_NAME="qwen-plus",
        MODEL_API_KEY="test-api-key",
    )
    assert isinstance(build_chat_model(settings), DashScopeChatModel)


def test_openai_compatible_model_uses_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="openai_compatible",
        MODEL_NAME="compatible-model",
        MODEL_API_KEY="test-api-key",
        MODEL_BASE_URL="https://model.example.test/v1",
    )
    assert isinstance(build_chat_model(settings), OpenAIChatModel)


def test_openai_compatible_model_can_disable_provider_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="openai_compatible",
        MODEL_NAME="qwen3.6-flash",
        MODEL_API_KEY="test-api-key",
        MODEL_BASE_URL="https://model.example.test/v1",
        MODEL_ENABLE_THINKING="false",
    )
    model = build_chat_model(settings)
    assert isinstance(model, OpenAIChatModel)
    assert model.extra_body == {"enable_thinking": False}


def test_openai_model_does_not_receive_compatible_provider_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="openai",
        MODEL_NAME="openai-model",
        MODEL_API_KEY="test-api-key",
        MODEL_ENABLE_THINKING="false",
    )
    model = build_chat_model(settings)
    assert isinstance(model, OpenAIChatModel)
    assert model.extra_body is None


def test_missing_api_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="dashscope",
        MODEL_NAME="qwen-plus",
    )
    with pytest.raises(ModelConfigurationError, match="MODEL_API_KEY"):
        build_chat_model(settings)


def test_configured_factory_applies_environment_to_all_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="dashscope",
        MODEL_NAME="qwen-plus",
        MODEL_API_KEY="test-api-key",
        MODEL_MAX_RETRIES="2",
    )
    bundle = ConfiguredAgentFactory(settings).build(
        orchestrator_toolkit=Toolkit(),
        data_toolkit=Toolkit(),
        action_toolkit=Toolkit(),
        patrol_toolkit=Toolkit(),
    )
    assert bundle.data_agent.model is bundle.action_agent.model
    assert bundle.data_agent.model_config.max_retries == 2
    assert bundle.orchestrator.name == "orchestrator"
    summary_agent = ConfiguredAgentFactory(settings).build_summary_agent()
    assert summary_agent.name == "conversation_summarizer"
    assert summary_agent.model_config.max_retries == 2


def test_invalid_summary_message_window_fails_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="CHAT_SUMMARY_KEEP_RECENT_MESSAGES"):
        settings_from_environment(
            monkeypatch,
            CHAT_SUMMARY_TRIGGER_MESSAGES="8",
            CHAT_SUMMARY_KEEP_RECENT_MESSAGES="8",
        )


def test_patrol_budget_defaults_and_configured_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="openai",
        MODEL_NAME="openai-model",
        MODEL_API_KEY="test-api-key",
    )
    assert settings.patrol_max_iterations == 12
    assert settings.patrol_turn_timeout_seconds == 150.0

    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="openai",
        MODEL_NAME="openai-model",
        MODEL_API_KEY="test-api-key",
        PATROL_MAX_ITERATIONS="16",
        PATROL_TURN_TIMEOUT_SECONDS="200",
    )
    bundle = ConfiguredAgentFactory(settings).build(
        orchestrator_toolkit=Toolkit(),
        data_toolkit=Toolkit(),
        action_toolkit=Toolkit(),
        patrol_toolkit=Toolkit(),
    )
    assert bundle.patrol_agent.react_config.max_iters == 16
    assert bundle.data_agent.react_config.max_iters == 8
    assert bundle.action_agent.react_config.max_iters == 8


def test_business_agents_receive_system_prompt_date_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        monkeypatch,
        MODEL_PROVIDER="openai",
        MODEL_NAME="openai-model",
        MODEL_API_KEY="test-api-key",
    )
    bundle = ConfiguredAgentFactory(settings).build(
        orchestrator_toolkit=Toolkit(),
        data_toolkit=Toolkit(),
        action_toolkit=Toolkit(),
        patrol_toolkit=Toolkit(),
    )
    for agent in (bundle.data_agent, bundle.action_agent, bundle.patrol_agent):
        # AgentScope stores the prompt privately; assert on the composed value.
        assert "运行时日期上下文（服务器时钟" in agent._system_prompt
        assert "今天是" in agent._system_prompt
    # The tool-free orchestrator must not carry ERP runtime context.
    assert "运行时日期上下文（服务器时钟" not in bundle.orchestrator._system_prompt


def test_patrol_timeout_below_base_fails_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="PATROL_TURN_TIMEOUT_SECONDS"):
        settings_from_environment(
            monkeypatch,
            AGENT_TURN_TIMEOUT_SECONDS="120",
            PATROL_TURN_TIMEOUT_SECONDS="60",
        )


def test_action_recovery_configuration_fails_for_unsafe_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="ACTION_RECOVERY_RETRY_SECONDS"):
        settings_from_environment(
            monkeypatch,
            ACTION_RECOVERY_POLL_SECONDS="60",
            ACTION_RECOVERY_RETRY_SECONDS="30",
        )

    with pytest.raises(ValueError, match="ACTION_EXECUTION_LOCK_TTL_SECONDS"):
        settings_from_environment(
            monkeypatch,
            ACTION_RECOVERY_POLL_SECONDS="15",
            ACTION_RECOVERY_RETRY_SECONDS="60",
            ACTION_EXECUTION_LOCK_TTL_SECONDS="60",
        )
