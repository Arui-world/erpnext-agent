from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from time import perf_counter
from typing import Any, Final
from uuid import UUID

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue

from erpnext_agent import __version__
from erpnext_agent.config import Settings

_TECHNICAL_VALUE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class Telemetry:
    """Small, privacy-constrained facade around OpenTelemetry tracing."""

    def __init__(
        self,
        *,
        enabled: bool,
        tracer: Tracer,
        identifier_secret: str,
        provider: TracerProvider | None = None,
    ) -> None:
        self.enabled = enabled
        self._tracer = tracer
        self._identifier_secret = identifier_secret.encode("utf-8")
        self._provider = provider

    @classmethod
    def disabled(cls, identifier_secret: str | None = None) -> Telemetry:
        provider = trace.NoOpTracerProvider()
        return cls(
            enabled=False,
            tracer=provider.get_tracer("erpnext_agent"),
            identifier_secret=identifier_secret or "telemetry-disabled",
        )

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: Mapping[str, AttributeValue] | None = None,
        parent_context: Context | None = None,
    ) -> Iterator[Span]:
        started = perf_counter()
        with self._tracer.start_as_current_span(
            name,
            context=parent_context,
            attributes=dict(attributes or {}),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException:
                status = getattr(span, "status", None)
                if status is None or status.status_code is StatusCode.UNSET:
                    set_span_result(span, "UNHANDLED_EXCEPTION", error=True)
                raise
            finally:
                if span.is_recording():
                    span.set_attribute(
                        "duration_ms",
                        round((perf_counter() - started) * 1000, 3),
                    )

    def identifier(self, value: str) -> str:
        return hmac.new(
            self._identifier_secret,
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def extract(headers: Mapping[str, str]) -> Context:
        # Deliberately propagate only W3C trace context. Arbitrary inbound baggage
        # must not be copied to ERPNext MCP requests.
        return TraceContextTextMapPropagator().extract(headers)

    @staticmethod
    def current_context() -> Context:
        return otel_context.get_current()

    @staticmethod
    def inject(headers: MutableMapping[str, str]) -> None:
        TraceContextTextMapPropagator().inject(headers)

    def force_flush(self, timeout_millis: int = 5_000) -> bool:
        if self._provider is None:
            return True
        return self._provider.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()


def create_telemetry(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    use_batch_processor: bool = True,
) -> Telemetry:
    secret = settings.session_secret.get_secret_value()
    if not settings.otel_enabled:
        return Telemetry.disabled(secret)

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": __version__,
                "deployment.environment.name": settings.app_env,
            }
        ),
        shutdown_on_exit=False,
    )
    exporter = span_exporter
    if exporter is None:
        assert settings.otel_exporter_otlp_endpoint is not None
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            timeout=settings.otel_export_timeout_seconds,
        )
    if use_batch_processor:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        provider.add_span_processor(SimpleSpanProcessor(exporter))
    return Telemetry(
        enabled=True,
        tracer=provider.get_tracer("erpnext_agent", __version__),
        identifier_secret=secret,
        provider=provider,
    )


def set_span_result(span: Span, code: str, *, error: bool = False) -> None:
    if not span.is_recording():
        return
    span.set_attribute("result_code", normalized_technical_value(code))
    span.set_status(Status(StatusCode.ERROR if error else StatusCode.OK))


def normalized_request_id(value: str | None, *, fallback: str) -> str:
    if value is not None:
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return fallback


def normalized_technical_value(value: str, *, fallback: str = "INVALID_VALUE") -> str:
    return value if _TECHNICAL_VALUE.fullmatch(value) else fallback


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[None]:
    """Backward-compatible no-op seam for callers not yet bound to app telemetry."""

    del name, attributes
    yield
