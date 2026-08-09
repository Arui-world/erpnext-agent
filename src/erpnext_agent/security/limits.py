from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_react_iterations: int = 8
    max_tool_calls: int = 12
    max_rows: int = 500
    max_pages: int = 5
    max_turn_seconds: int = 90
    max_message_chars: int = 16_000

