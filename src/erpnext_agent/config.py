from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urljoin, urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-only service configuration.

    Secret values intentionally have no usable defaults. A deployment must supply them
    through the environment or a secret manager.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "ERPNext Agent"
    app_env: Literal["development", "test", "production"] = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"  # noqa: S104 - required inside a container
    app_port: int = Field(default=8001, ge=1, le=65535)
    app_base_url: str = "http://localhost:8001"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    trusted_hosts: str = "localhost,127.0.0.1,agent"
    cors_origins: str = "http://localhost:3000,http://localhost:8001"

    database_url: str
    redis_url: SecretStr

    erpnext_base_url: str
    erpnext_internal_url: str | None = None
    erpnext_mcp_url: str | None = None
    erpnext_site: str
    mcp_http_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    mcp_execution_timeout_seconds: float = Field(default=25.0, gt=0, le=180)
    mcp_verify_tool_contract: bool = True
    mcp_loop_guard_max_repeats: int = Field(default=3, ge=2, le=8)

    oauth_client_id: str
    oauth_client_secret: SecretStr
    oauth_token_endpoint_auth_method: Literal[
        "client_secret_basic",
        "client_secret_post",
    ] = "client_secret_post"  # noqa: S105 - OAuth method identifier
    oauth_redirect_uri: str
    oauth_scope: str = "all openid"
    oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    oauth_refresh_leeway_seconds: int = Field(default=120, ge=0, le=3600)
    oauth_refresh_lock_ttl_seconds: int = Field(default=60, ge=10, le=300)
    oauth_refresh_wait_seconds: float = Field(default=25.0, gt=0, le=120)
    oauth_refresh_poll_seconds: float = Field(default=0.1, gt=0, le=2)
    oauth_introspection_cache_seconds: int = Field(default=5, ge=0, le=60)
    oauth_logout_event_max_skew_seconds: int = Field(default=60, ge=10, le=600)
    oauth_logout_event_replay_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    erp_logout_webhook_secret: SecretStr | None = None

    session_cookie_name: str = "erpnext_agent_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    session_secret: SecretStr
    token_encryption_key: SecretStr
    token_encryption_key_version: str = "v1"  # noqa: S105 - identifier, not a secret

    action_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    action_recovery_enabled: bool = True
    action_recovery_poll_seconds: int = Field(default=15, ge=1, le=3600)
    action_recovery_retry_seconds: int = Field(default=60, ge=5, le=86_400)
    action_recovery_batch_size: int = Field(default=20, ge=1, le=200)
    action_execution_lock_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    action_history_limit: int = Field(default=50, ge=1, le=200)

    chat_history_max_messages: int = Field(default=20, ge=2, le=100)
    chat_history_max_chars: int = Field(default=24_000, ge=4_000, le=100_000)
    chat_history_display_limit: int = Field(default=100, ge=10, le=500)
    chat_conversation_list_limit: int = Field(default=50, ge=10, le=200)
    chat_summary_enabled: bool = True
    chat_summary_trigger_messages: int = Field(default=16, ge=4, le=200)
    chat_summary_trigger_chars: int = Field(default=16_000, ge=2_000, le=100_000)
    chat_summary_keep_recent_messages: int = Field(default=8, ge=2, le=100)
    chat_summary_source_max_chars: int = Field(default=24_000, ge=4_000, le=100_000)
    chat_summary_max_chars: int = Field(default=4_000, ge=500, le=20_000)
    chat_summary_lock_ttl_seconds: int = Field(default=120, ge=30, le=600)
    chat_retention_enabled: bool = True
    chat_retention_days: int = Field(default=180, ge=1, le=3650)
    chat_deleted_retention_days: int = Field(default=7, ge=1, le=365)
    chat_empty_retention_hours: int = Field(default=24, ge=1, le=720)
    chat_retention_sweep_seconds: int = Field(default=3600, ge=60, le=86_400)
    chat_retention_batch_size: int = Field(default=100, ge=1, le=1000)

    model_provider: Literal["dashscope", "openai", "openai_compatible"] = "openai_compatible"
    model_name: str = ""
    model_api_key: SecretStr | None = None
    model_base_url: str | None = None
    model_max_retries: int = Field(default=1, ge=0, le=3)

    otel_enabled: bool = False
    otel_service_name: str = "erpnext-agent"
    otel_exporter_otlp_endpoint: str | None = None
    otel_export_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    @field_validator("app_base_url", "erpnext_base_url", "oauth_redirect_uri")
    @classmethod
    def strip_url_suffix(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("api_prefix")
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        return "/" + value.strip("/")

    @field_validator(
        "erpnext_internal_url",
        "model_base_url",
        "otel_exporter_otlp_endpoint",
        mode="before",
    )
    @classmethod
    def empty_string_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        return value

    @field_validator("token_encryption_key")
    @classmethod
    def validate_fernet_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc
        return value

    @field_validator("erp_logout_webhook_secret")
    @classmethod
    def validate_logout_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("ERP_LOGOUT_WEBHOOK_SECRET must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_environment_safety(self) -> Settings:
        if self.otel_enabled and self.otel_exporter_otlp_endpoint is None:
            raise ValueError(
                "OTEL_EXPORTER_OTLP_ENDPOINT is required when OTEL_ENABLED=true"
            )
        if self.action_recovery_retry_seconds < self.action_recovery_poll_seconds:
            raise ValueError(
                "ACTION_RECOVERY_RETRY_SECONDS must be greater than or equal to "
                "ACTION_RECOVERY_POLL_SECONDS"
            )
        minimum_execution_lease = (
            (6 * self.mcp_http_timeout_seconds) + self.oauth_refresh_wait_seconds + 5
        )
        if self.action_execution_lock_ttl_seconds < minimum_execution_lease:
            raise ValueError(
                "ACTION_EXECUTION_LOCK_TTL_SECONDS is too short for the configured "
                "MCP and OAuth timeouts"
            )
        if self.chat_summary_keep_recent_messages >= self.chat_summary_trigger_messages:
            raise ValueError(
                "CHAT_SUMMARY_KEEP_RECENT_MESSAGES must be less than "
                "CHAT_SUMMARY_TRIGGER_MESSAGES"
            )
        if self.session_cookie_samesite == "none" and not self.session_cookie_secure:
            raise ValueError("SameSite=None requires SESSION_COOKIE_SECURE=true")
        if self.app_env == "production":
            if self.app_debug:
                raise ValueError("APP_DEBUG must be false in production")
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be true in production")
            if not self.app_base_url.startswith("https://"):
                raise ValueError("APP_BASE_URL must use HTTPS in production")
            if not self.oauth_redirect_uri.startswith("https://"):
                raise ValueError("OAUTH_REDIRECT_URI must use HTTPS in production")
            if self.erp_logout_webhook_secret is None:
                raise ValueError("ERP_LOGOUT_WEBHOOK_SECRET is required in production")
        return self

    @property
    def effective_erpnext_internal_url(self) -> str:
        return (self.erpnext_internal_url or self.erpnext_base_url).rstrip("/")

    @property
    def erpnext_host_header(self) -> str:
        host = urlsplit(self.erpnext_base_url).netloc
        if not host:
            raise ValueError("ERPNEXT_BASE_URL must contain a hostname")
        return host

    @property
    def effective_mcp_url(self) -> str:
        if self.erpnext_mcp_url:
            return self.erpnext_mcp_url
        return urljoin(
            self.effective_erpnext_internal_url + "/",
            "api/method/erpnext_mcp_tools.mcp.handle_mcp",
        )

    @property
    def trusted_host_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
