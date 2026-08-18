from __future__ import annotations

from typing import Any

import pytest

from erpnext_agent.security.limits import RedisRateLimiter, RequestBodyLimitMiddleware


class FakeRateRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.keys: list[str] = []

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[int]:
        del script
        assert numkeys == 1
        key = str(keys_and_args[0])
        ttl = int(keys_and_args[1])
        self.keys.append(key)
        self.counts[key] = self.counts.get(key, 0) + 1
        return [self.counts[key], ttl]


@pytest.mark.asyncio
async def test_rate_limiter_allows_then_denies_with_retry_after() -> None:
    redis = FakeRateRedis()
    limiter = RedisRateLimiter(redis, "test-secret")

    first = await limiter.check(
        bucket="chat",
        identity="session-secret-value",
        limit=2,
        window_seconds=60,
    )
    second = await limiter.check(
        bucket="chat",
        identity="session-secret-value",
        limit=2,
        window_seconds=60,
    )
    denied = await limiter.check(
        bucket="chat",
        identity="session-secret-value",
        limit=2,
        window_seconds=60,
    )

    assert first.allowed is True and first.remaining == 1
    assert second.allowed is True and second.remaining == 0
    assert denied.allowed is False and denied.retry_after_seconds == 60
    assert all("session-secret-value" not in key for key in redis.keys)


@pytest.mark.asyncio
async def test_rate_limiter_separates_buckets() -> None:
    redis = FakeRateRedis()
    limiter = RedisRateLimiter(redis, "test-secret")
    chat = await limiter.check(bucket="chat", identity="same", limit=1, window_seconds=60)
    action = await limiter.check(bucket="action", identity="same", limit=1, window_seconds=60)
    assert chat.allowed is True
    assert action.allowed is True
    assert len(set(redis.keys)) == 2


def _scope(*, content_length: int | None = None) -> dict[str, Any]:
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/chat",
        "raw_path": b"/api/v1/chat",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }


@pytest.mark.asyncio
async def test_request_body_limit_rejects_content_length_before_app() -> None:
    called = False

    async def app(scope, receive, send):
        nonlocal called
        del scope, receive, send
        called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    middleware = RequestBodyLimitMiddleware(app, max_bytes=4)
    await middleware(_scope(content_length=5), receive, send)  # type: ignore[arg-type]
    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_request_body_limit_rejects_chunked_body() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"de", "more_body": False},
        ]
    )

    async def receive():
        return next(messages)

    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        raise AssertionError("oversized body reached application")

    middleware = RequestBodyLimitMiddleware(app, max_bytes=4)
    await middleware(_scope(), receive, send)  # type: ignore[arg-type]
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_request_body_limit_replays_valid_body() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    observed: list[bytes] = []

    async def send(message):
        del message

    async def app(scope, receive, send):
        del scope, send
        observed.append((await receive())["body"])
        assert (await receive())["type"] == "http.disconnect"

    middleware = RequestBodyLimitMiddleware(app, max_bytes=4)
    await middleware(_scope(), receive, send)  # type: ignore[arg-type]
    assert observed == [b"abc"]
