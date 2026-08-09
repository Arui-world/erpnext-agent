from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urljoin

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
    auto_create_schema: bool = True

    database_url: str
    redis_url: SecretStr

    erpnext_base_url: str
    erpnext_mcp_url: str | None = None
    erpnext_site: str
    mcp_http_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    mcp_execution_timeout_seconds: float = Field(default=25.0, gt=0, le=180)
    mcp_verify_tool_contract: bool = True

    oauth_client_id: str
    oauth_client_secret: SecretStr
    oauth_redirect_uri: str
    oauth_scope: str = "all openid"
    oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=1800)

    session_cookie_name: str = "erpnext_agent_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    session_secret: SecretStr
    token_encryption_key: SecretStr
    token_encryption_key_version: str = "v1"  # noqa: S105 - identifier, not a secret

    action_ttl_seconds: int = Field(default=900, ge=60, le=86_400)

    model_provider: Literal["dashscope", "openai", "openai_compatible"] = "openai_compatible"
    model_name: str = ""
    model_api_key: SecretStr | None = None
    model_base_url: str | None = None
    model_max_retries: int = Field(default=1, ge=0, le=3)

    otel_enabled: bool = False
    otel_service_name: str = "erpnext-agent"
    otel_exporter_otlp_endpoint: str | None = None

    @field_validator("app_base_url", "erpnext_base_url", "oauth_redirect_uri")
    @classmethod
    def strip_url_suffix(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("api_prefix")
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        return "/" + value.strip("/")

    @field_validator("model_base_url", "otel_exporter_otlp_endpoint", mode="before")
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

    @model_validator(mode="after")
    def validate_environment_safety(self) -> Settings:
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
            if self.auto_create_schema:
                raise ValueError("AUTO_CREATE_SCHEMA must be false in production")
        return self

    @property
    def effective_mcp_url(self) -> str:
        if self.erpnext_mcp_url:
            return self.erpnext_mcp_url
        return urljoin(
            self.erpnext_base_url + "/",
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
