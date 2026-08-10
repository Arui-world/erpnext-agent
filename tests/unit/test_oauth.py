import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from erpnext_agent.auth.oauth_client import OAuthClient, create_pkce_request
from erpnext_agent.config import Settings


def oauth_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]  # BaseSettings runtime option
        database_url="postgresql+asyncpg://agent:password@postgres/agent",
        redis_url=SecretStr("redis://:password@redis/0"),
        erpnext_base_url="http://dev.localhost:8000",
        erpnext_internal_url="http://frappe:8000",
        erpnext_site="dev.localhost",
        oauth_client_id="client-id",
        oauth_client_secret=SecretStr("client-secret"),  # noqa: S106
        oauth_redirect_uri="http://localhost:8001/api/v1/auth/callback",
        session_secret=SecretStr(  # noqa: S106
            "a-session-secret-with-at-least-32-characters",
        ),
        token_encryption_key=SecretStr(  # noqa: S106
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ),
    )


def test_pkce_challenge_is_s256_of_verifier() -> None:
    request = create_pkce_request()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(request.verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert request.challenge == expected
    assert request.state
    assert request.nonce


@pytest.mark.asyncio
async def test_oauth_separates_browser_and_container_urls() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "test-access-token",
                "refresh_token": "test-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "all openid",
            },
        )

    settings = oauth_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        oauth = OAuthClient(settings, http)
        authorization = urlsplit(oauth.authorization_url(create_pkce_request()))
        query = parse_qs(authorization.query)
        assert authorization.netloc == "dev.localhost:8000"
        assert query["redirect_uri"] == ["http://localhost:8001/api/v1/auth/callback"]

        token = await oauth.exchange_code(code="test-code", verifier="test-verifier")

    assert token.access_token == "test-access-token"  # noqa: S105
    assert captured[0].url == httpx.URL(
        "http://frappe:8000/api/method/frappe.integrations.oauth2.get_token"
    )
    assert captured[0].headers["host"] == "dev.localhost:8000"
    token_form = parse_qs(captured[0].content.decode())
    assert "authorization" not in captured[0].headers
    assert token_form["client_id"] == ["client-id"]
    assert token_form["client_secret"] == ["client-secret"]  # noqa: S105
    assert settings.effective_mcp_url == (
        "http://frappe:8000/api/method/erpnext_mcp_tools.mcp.handle_mcp"
    )


@pytest.mark.asyncio
async def test_fetch_logged_user_returns_canonical_frappe_user() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"message": "Administrator"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        oauth = OAuthClient(oauth_settings(), http)
        user = await oauth.fetch_logged_user("test-access-token")

    assert user == "Administrator"
    assert captured[0].url == httpx.URL(
        "http://frappe:8000/api/method/frappe.auth.get_logged_user"
    )
    assert captured[0].headers["host"] == "dev.localhost:8000"
    assert captured[0].headers["authorization"] == "Bearer test-access-token"
