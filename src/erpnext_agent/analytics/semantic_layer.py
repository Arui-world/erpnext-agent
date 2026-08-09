from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    aliases: tuple[str, ...]
    tool: str
    dimensions: tuple[str, ...]

