from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RedisRateLimitClient(Protocol):
    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> Awaitable[Any]: ...


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_react_iterations: int = 8
    max_tool_calls: int = 12
    max_rows: int = 500
    max_pages: int = 5
    max_turn_seconds: int = 90
    max_message_chars: int = 16_000


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


_FIXED_WINDOW_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
""".strip()


class RedisRateLimiter:
    """Distributed fixed-window limiter whose Redis keys contain no raw identity."""

    def __init__(self, redis: RedisRateLimitClient, secret: str) -> None:
        self._redis = redis
        self._secret = secret.encode()

    async def check(
        self,
        *,
        bucket: str,
        identity: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        digest = hmac.new(
            self._secret,
            f"{bucket}:{identity}".encode(),
            hashlib.sha256,
        ).hexdigest()
        key = f"agent:rate-limit:{bucket}:{digest}"
        raw = await self._redis.eval(
            _FIXED_WINDOW_SCRIPT,
            1,
            key,
            window_seconds,
        )
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise RuntimeError("Redis returned an invalid rate-limit result")
        count = int(raw[0])
        ttl = max(1, int(raw[1]))
        return RateLimitDecision(
            allowed=count <= limit,
            remaining=max(0, limit - count),
            retry_after_seconds=ttl,
        )


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP request bodies before FastAPI parses JSON or form data."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http" or scope.get("method") not in {
            "POST",
            "PUT",
            "PATCH",
        }:
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        buffered: list[Message] = []
        received = 0
        more_body = True
        while more_body:
            message = await receive()
            buffered.append(message)
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    received += len(body)
                more_body = bool(message.get("more_body", False))
                if received > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            else:
                more_body = False

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "code": "REQUEST_TOO_LARGE",
                    "message": "Request body is too large",
                }
            },
        )
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None
