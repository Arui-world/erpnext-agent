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
    for name in ("MODEL_PROVIDER", "MODEL_NAME", "MODEL_API_KEY", "MODEL_BASE_URL"):
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
