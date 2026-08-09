from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from erpnext_agent.config import Settings


class OAuthError(RuntimeError):
    """A safe OAuth error whose message contains no token material."""


@dataclass(frozen=True, slots=True)
class PKCERequest:
    state: str
    nonce: str
    verifier: str
    challenge: str


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str | None
    token_type: str
    scope: str
    expires_in: int | None


def create_pkce_request() -> PKCERequest:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PKCERequest(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        verifier=verifier,
        challenge=challenge,
    )


class OAuthClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    @property
    def authorize_endpoint(self) -> str:
        return urljoin(
            self._settings.erpnext_base_url + "/",
            "api/method/frappe.integrations.oauth2.authorize",
        )

    @property
    def token_endpoint(self) -> str:
        return urljoin(
            self._settings.erpnext_base_url + "/",
            "api/method/frappe.integrations.oauth2.get_token",
        )

    @property
    def profile_endpoint(self) -> str:
        return urljoin(
            self._settings.erpnext_base_url + "/",
            "api/method/frappe.integrations.oauth2.openid_profile",
        )

    @property
    def revoke_endpoint(self) -> str:
        return urljoin(
            self._settings.erpnext_base_url + "/",
            "api/method/frappe.integrations.oauth2.revoke_token",
        )

    def authorization_url(self, request: PKCERequest) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.oauth_client_id,
                "redirect_uri": self._settings.oauth_redirect_uri,
                "scope": self._settings.oauth_scope,
                "state": request.state,
                "nonce": request.nonce,
                "code_challenge": request.challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.authorize_endpoint}?{query}"

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.oauth_redirect_uri,
                "code_verifier": verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def _token_request(self, form: dict[str, str]) -> OAuthTokenSet:
        try:
            response = await self._http.post(
                self.token_endpoint,
                data=form,
                auth=httpx.BasicAuth(
                    self._settings.oauth_client_id,
                    self._settings.oauth_client_secret.get_secret_value(),
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("ERPNext token endpoint rejected the request") from exc

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthError("ERPNext token response did not contain an access token")
        expires_in = payload.get("expires_in")
        return OAuthTokenSet(
            access_token=access_token,
            refresh_token=_optional_string(payload.get("refresh_token")),
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=str(payload.get("scope") or self._settings.oauth_scope),
            expires_in=int(expires_in) if expires_in is not None else None,
        )

    async def fetch_profile(self, access_token: str) -> dict[str, Any]:
        try:
            response = await self._http.get(
                self.profile_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("Unable to verify the ERPNext OAuth identity") from exc
        if not isinstance(payload, dict):
            raise OAuthError("ERPNext profile response was invalid")
        return payload

    async def revoke(self, access_token: str) -> None:
        try:
            response = await self._http.post(
                self.revoke_endpoint,
                data={"token": access_token},
                auth=httpx.BasicAuth(
                    self._settings.oauth_client_id,
                    self._settings.oauth_client_secret.get_secret_value(),
                ),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OAuthError("ERPNext token revocation failed") from exc


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

