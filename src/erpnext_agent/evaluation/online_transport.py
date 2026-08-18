"""Chat transport seam for the authenticated online evaluation executor.

``ChatTransport`` is the single injection point that lets unit tests drive the
online executor with a fake transport while production uses ``HttpChatTransport``
against the live deployment. ``parse_chat_stream`` is a standalone parser over raw
SSE lines so it can be exercised with fake async generators and no network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from erpnext_agent.auth.session_store import AgentSession


@dataclass(frozen=True, slots=True)
class LiveStreamReply:
    """Normalized result of one streamed chat turn."""

    conversation_id: str | None = None
    route: str | None = None
    text: str = ""
    tool_calls: tuple[str, ...] = ()
    action: dict[str, Any] | None = None
    finished_reason: str | None = None
    error: dict[str, Any] | None = None


class ChatTransport(Protocol):
    def stream_chat(
        self,
        *,
        session: AgentSession,
        message: str,
        conversation_id: str | None,
    ) -> AsyncIterator[str]:
        """Yield raw SSE lines for a single chat turn.

        Declared as a regular method returning an async iterator because async
        generator implementations produce the iterator synchronously when called.
        """
        ...

    async def post_json(
        self,
        *,
        session: AgentSession,
        path: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        """POST to a protected API path and return (status_code, json_body)."""
        ...


async def parse_chat_stream(lines: AsyncIterator[str]) -> LiveStreamReply:
    """Parse raw SSE lines into a :class:`LiveStreamReply`.

    Mirrors the line-oriented parsing used by ``stock_smoke``: lines beginning with
    ``event:`` set the current event name, lines beginning with ``data:`` carry a JSON
    payload, and every other line (including blank separators) is ignored.
    """
    event_name = "message"
    conversation_id: str | None = None
    route: str | None = None
    text_chunks: list[str] = []
    tool_calls: list[str] = []
    action: dict[str, Any] | None = None
    finished_reason: str | None = None
    error: dict[str, Any] | None = None

    async for line in lines:
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
            continue
        if not line.startswith("data:"):
            continue
        payload = _loads(line.removeprefix("data:").strip())
        if payload is None:
            continue
        if event_name == "conversation":
            value = payload.get("conversation_id")
            if isinstance(value, str) and value:
                conversation_id = value
        elif event_name == "route":
            value = payload.get("route")
            if isinstance(value, str) and value:
                route = value
        elif event_name == "tool_call_start":
            value = payload.get("tool_name")
            if isinstance(value, str) and value:
                tool_calls.append(value)
        elif event_name == "text_delta":
            value = payload.get("delta")
            if isinstance(value, str):
                text_chunks.append(value)
        elif event_name == "action_required":
            action = payload
        elif event_name == "done":
            reason = payload.get("finished_reason") or payload.get("status")
            finished_reason = str(reason) if reason is not None else "done"
        elif event_name == "error":
            error = payload

    return LiveStreamReply(
        conversation_id=conversation_id,
        route=route,
        text="".join(text_chunks).strip(),
        tool_calls=tuple(tool_calls),
        action=action,
        finished_reason=finished_reason,
        error=error,
    )


def _loads(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


class HttpChatTransport:
    """Real transport that calls the running Agent HTTP API."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        api_prefix: str,
        cookie_name: str,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_prefix = api_prefix
        self._cookie_name = cookie_name
        self._timeout_seconds = timeout_seconds

    async def stream_chat(
        self,
        *,
        session: AgentSession,
        message: str,
        conversation_id: str | None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {"message": message}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        async with self._client.stream(
            "POST",
            f"{self._base_url}{self._api_prefix}/chat/stream",
            headers=self._protected_headers(session),
            cookies={self._cookie_name: session.session_id},
            json=payload,
            timeout=self._timeout_seconds,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line

    async def post_json(
        self,
        *,
        session: AgentSession,
        path: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        response = await self._client.post(
            f"{self._base_url}{self._api_prefix}{path}",
            headers=self._protected_headers(session),
            cookies={self._cookie_name: session.session_id},
            json=payload or {},
            timeout=self._timeout_seconds,
        )
        return response.status_code, _safe_json(response)

    def _protected_headers(self, session: AgentSession) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": session.csrf_token,
        }


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        loaded = response.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
