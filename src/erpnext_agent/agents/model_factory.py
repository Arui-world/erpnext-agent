from __future__ import annotations

from agentscope.credential import DashScopeCredential, OpenAICredential
from agentscope.model import ChatModelBase, DashScopeChatModel, OpenAIChatModel

from erpnext_agent.config import Settings


class ModelConfigurationError(ValueError):
    """Raised when a model cannot be built from the configured environment."""


def validate_model_configuration(settings: Settings) -> None:
    model_name = settings.model_name.strip()
    if not model_name or model_name == "replace-with-model-name":
        raise ModelConfigurationError("MODEL_NAME must be configured")

    api_key = settings.model_api_key
    if api_key is None or not api_key.get_secret_value().strip():
        raise ModelConfigurationError("MODEL_API_KEY must be configured")

    base_url = settings.model_base_url
    if base_url is not None:
        base_url = base_url.strip() or None
    if settings.model_provider == "openai_compatible" and not base_url:
        raise ModelConfigurationError(
            "MODEL_BASE_URL is required for an OpenAI-compatible provider"
        )


def build_chat_model(settings: Settings) -> ChatModelBase:
    """Build an AgentScope 2.0.5 chat model from the validated Settings.

    `MODEL_MAX_RETRIES` belongs to the Agent-level fallback/retry policy. The
    provider client therefore gets zero inner retries so that failures are not
    retried by two nested layers.
    """

    validate_model_configuration(settings)
    model_name = settings.model_name.strip()
    api_key = settings.model_api_key
    assert api_key is not None  # narrowed by validate_model_configuration
    base_url = settings.model_base_url
    if base_url is not None:
        base_url = base_url.strip() or None

    if settings.model_provider == "dashscope":
        dashscope_credential = (
            DashScopeCredential(api_key=api_key, base_url=base_url)
            if base_url
            else DashScopeCredential(api_key=api_key)
        )
        return DashScopeChatModel(
            credential=dashscope_credential,
            model=model_name,
            stream=True,
            max_retries=0,
        )

    if settings.model_provider in {"openai", "openai_compatible"}:
        openai_credential = OpenAICredential(
            api_key=api_key,
            base_url=base_url,
        )
        return OpenAIChatModel(
            credential=openai_credential,
            model=model_name,
            stream=True,
            max_retries=0,
        )

    # Settings validates this already; this branch protects direct/future callers.
    raise ModelConfigurationError(f"Unsupported MODEL_PROVIDER: {settings.model_provider}")


def model_configuration_is_complete(settings: Settings) -> bool:
    try:
        validate_model_configuration(settings)
    except ModelConfigurationError:
        return False
    return True
