from __future__ import annotations

from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "client_secret",
        "code_verifier",
        "session_id",
    }
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.casefold() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value

