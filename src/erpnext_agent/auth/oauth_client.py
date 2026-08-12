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


@dataclass(frozen=True, slots=True)
class OAuthBindingConfirmation:
    binding_id: str
    user: str
    client_id: str


@dataclass(frozen=True, slots=True)
class OAuthIntrospection:
    active: bool
    client_id: str | None


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
            self._settings.effective_erpnext_internal_url + "/",
            "api/method/frappe.integrations.oauth2.get_token",
        )

    @property
    def binding_start_endpoint(self) -> str:
        return urljoin(
            self._settings.erpnext_base_url + "/",
            "api/method/erpnext_mcp_tools.auth.device_binding.begin",
        )

    @property
    def binding_confirm_endpoint(self) -> str:
        return urljoin(
            self._settings.effective_erpnext_internal_url + "/",
            "api/method/erpnext_mcp_tools.auth.device_binding.confirm",
        )

    @property
    def profile_endpoint(self) -> str:
        return urljoin(
            self._settings.effective_erpnext_internal_url + "/",
            "api/method/frappe.integrations.oauth2.openid_profile",
        )

    @property
    def logged_user_endpoint(self) -> str:
        return urljoin(
            self._settings.effective_erpnext_internal_url + "/",
            "api/method/frappe.auth.get_logged_user",
        )

    @property
    def revoke_endpoint(self) -> str:
        return urljoin(
            self._settings.effective_erpnext_internal_url + "/",
            "api/method/frappe.integrations.oauth2.revoke_token",
        )

    @property
    def introspection_endpoint(self) -> str:
        return urljoin(
            self._settings.effective_erpnext_internal_url + "/",
            "api/method/frappe.integrations.oauth2.introspect_token",
        )

    def authorization_url(self, request: PKCERequest, *, binding_id: str) -> str:
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
                "binding_id": binding_id,
            }
        )
        return f"{self.binding_start_endpoint}?{query}"

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
            response = await self._client_authenticated_post(self.token_endpoint, form)
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
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Host": self._settings.erpnext_host_header,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("Unable to verify the ERPNext OAuth identity") from exc
        if not isinstance(payload, dict):
            raise OAuthError("ERPNext profile response was invalid")
        return payload

    async def fetch_logged_user(self, access_token: str) -> str:
        """Return the canonical Frappe User ID bound to an OAuth access token."""
        try:
            response = await self._http.get(
                self.logged_user_endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Host": self._settings.erpnext_host_header,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("Unable to verify the ERPNext session identity") from exc
        user = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(user, str) or not user:
            raise OAuthError("ERPNext session did not contain a user identity")
        return user

    async def confirm_binding(
        self,
        access_token: str,
        *,
        binding_id: str,
    ) -> OAuthBindingConfirmation:
        try:
            response = await self._http.post(
                self.binding_confirm_endpoint,
                data={"binding_id": binding_id},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Host": self._settings.erpnext_host_header,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("Unable to bind OAuth authorization to this browser") from exc
        data = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise OAuthError("ERPNext browser binding response was invalid")
        binding = data.get("binding_id")
        user = data.get("user")
        client_id = data.get("client_id")
        if not isinstance(binding, str) or not binding:
            raise OAuthError("ERPNext browser binding identity was invalid")
        if not isinstance(user, str) or not user:
            raise OAuthError("ERPNext browser binding identity was invalid")
        if not isinstance(client_id, str) or not client_id:
            raise OAuthError("ERPNext browser binding identity was invalid")
        return OAuthBindingConfirmation(binding, user, client_id)

    async def introspect(self, access_token: str) -> OAuthIntrospection:
        try:
            response = await self._client_authenticated_post(
                self.introspection_endpoint,
                {"token": access_token, "token_type_hint": "access_token"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("ERPNext token introspection is unavailable") from exc
        data = payload.get("message", payload) if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("active"), bool):
            raise OAuthError("ERPNext token introspection response was invalid")
        client_id = data.get("client_id")
        return OAuthIntrospection(
            active=data["active"],
            client_id=client_id if isinstance(client_id, str) else None,
        )

    async def revoke(self, access_token: str) -> None:
        try:
            response = await self._client_authenticated_post(
                self.revoke_endpoint,
                {"token": access_token},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OAuthError("ERPNext token revocation failed") from exc

    async def _client_authenticated_post(
        self,
        endpoint: str,
        form: dict[str, str],
    ) -> httpx.Response:
        headers = {"Host": self._settings.erpnext_host_header}
        if (
            self._settings.oauth_token_endpoint_auth_method == "client_secret_basic"  # noqa: S105
        ):
            return await self._http.post(
                endpoint,
                data=form,
                headers=headers,
                auth=httpx.BasicAuth(
                    self._settings.oauth_client_id,
                    self._settings.oauth_client_secret.get_secret_value(),
                ),
            )
        request_form = {
            **form,
            "client_id": self._settings.oauth_client_id,
            "client_secret": self._settings.oauth_client_secret.get_secret_value(),
        }
        return await self._http.post(endpoint, data=request_form, headers=headers)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
