from __future__ import annotations

import json

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr, ValidationError

from erpnext_agent.config import Settings
from erpnext_agent.mcp.adapter import ERPNextMCPAdapter
from erpnext_agent.observability import (
    create_telemetry,
    normalized_request_id,
    normalized_technical_value,
    set_span_result,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://agent:password@postgres/agent",
        "redis_url": SecretStr("redis://:password@redis/0"),
        "erpnext_base_url": "http://dev.localhost:8000",
        "erpnext_site": "dev.localhost",
        "oauth_client_id": "client-id",
        "oauth_client_secret": SecretStr("client-secret"),  # noqa: S106
        "oauth_redirect_uri": "http://localhost:8001/api/v1/auth/callback",
        "session_secret": SecretStr(  # noqa: S106
            "a-session-secret-with-at-least-32-characters"
        ),
        "token_encryption_key": SecretStr(  # noqa: S106
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        ),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_enabled_telemetry_requires_explicit_otlp_endpoint() -> None:
    with pytest.raises(ValidationError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        _settings(otel_enabled=True)


def test_identifier_is_stable_hmac_and_request_id_is_bounded() -> None:
    telemetry = create_telemetry(_settings())

    first = telemetry.identifier("raw-session-id")

    assert first == telemetry.identifier("raw-session-id")
    assert first != telemetry.identifier("another-session-id")
    assert "raw-session-id" not in first
    request_id = "00000000-0000-4000-8000-000000000001"
    assert normalized_request_id(request_id, fallback="fallback") == request_id
    assert normalized_request_id("has spaces", fallback="fallback") == "fallback"
    assert normalized_request_id("x" * 129, fallback="fallback") == "fallback"
    assert normalized_technical_value("MCP_AUTH_FAILED") == "MCP_AUTH_FAILED"
    assert normalized_technical_value("secret value with spaces") == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_mcp_spans_propagate_traceparent_without_sensitive_payloads() -> None:
    exporter = InMemorySpanExporter()
    telemetry = create_telemetry(
        _settings(
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://collector:4318/v1/traces",
        ),
        span_exporter=exporter,
        use_batch_processor=False,
    )
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "structuredContent": {
                        "ok": True,
                        "data": {"secret_business_field": "do-not-export"},
                        "meta": {"trace_id": "mcp-safe-trace"},
                    },
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = ERPNextMCPAdapter(
            url="http://frappe:8000/api/method/mcp",
            http=http,
            telemetry=telemetry,
        )
        with telemetry.span(
            "http.request",
            attributes={"request_id": "request-1"},
        ) as root:
            envelope = await adapter.call_tool(
                access_token="oauth-token-must-not-export",  # noqa: S106
                name="erpnext_get_list",
                arguments={"filters": {"private": "tool-argument-must-not-export"}},
            )
            set_span_result(root, "HTTP_200")

    spans = exporter.get_finished_spans()
    serialized_attributes = json.dumps(
        [dict(span.attributes) for span in spans],
        ensure_ascii=False,
    )

    assert envelope.meta["trace_id"] == "mcp-safe-trace"
    assert {span.name for span in spans} == {
        "http.request",
        "mcp.tool.call",
        "mcp.rpc",
    }
    assert len({span.context.trace_id for span in spans}) == 1
    assert captured[0].headers["traceparent"].startswith("00-")
    assert "baggage" not in captured[0].headers
    assert captured[0].headers["authorization"] == "Bearer oauth-token-must-not-export"
    assert "oauth-token-must-not-export" not in serialized_attributes
    assert "tool-argument-must-not-export" not in serialized_attributes
    assert "do-not-export" not in serialized_attributes
    assert "mcp-safe-trace" in serialized_attributes
    assert all("duration_ms" in span.attributes for span in spans)
    telemetry.shutdown()


def test_span_exception_does_not_export_exception_message_or_stack() -> None:
    exporter = InMemorySpanExporter()
    telemetry = create_telemetry(
        _settings(
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://collector:4318/v1/traces",
        ),
        span_exporter=exporter,
        use_batch_processor=False,
    )

    with pytest.raises(RuntimeError, match="secret-exception-value"):
        with telemetry.span("safe.operation") as span:
            set_span_result(span, "SAFE_FAILURE", error=True)
            raise RuntimeError("secret-exception-value")

    finished = exporter.get_finished_spans()[0]
    assert finished.events == ()
    assert finished.attributes["result_code"] == "SAFE_FAILURE"
    assert "secret-exception-value" not in json.dumps(dict(finished.attributes))
    telemetry.shutdown()


def test_unhandled_span_exception_gets_generic_code_without_details() -> None:
    exporter = InMemorySpanExporter()
    telemetry = create_telemetry(
        _settings(
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://collector:4318/v1/traces",
        ),
        span_exporter=exporter,
        use_batch_processor=False,
    )

    with pytest.raises(RuntimeError, match="private-runtime-value"):
        with telemetry.span("safe.unhandled"):
            raise RuntimeError("private-runtime-value")

    finished = exporter.get_finished_spans()[0]
    assert finished.events == ()
    assert finished.attributes["result_code"] == "UNHANDLED_EXCEPTION"
    assert "private-runtime-value" not in json.dumps(dict(finished.attributes))
    telemetry.shutdown()
