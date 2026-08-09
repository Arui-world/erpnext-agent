from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[None]:
    """Small stable seam for OpenTelemetry wiring in the hardening phase."""

    del name, attributes
    yield
