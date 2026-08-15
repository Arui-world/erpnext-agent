"""Pure assertion helpers for the authenticated online evaluation executor.

Every function is I/O-free and returns ``(passed, details)`` so callers can embed
the details dictionary directly into case evidence. Assertions intentionally avoid
depending on tool result payloads because the streaming ``tool_result_end`` event
carries only a ``tool_call_id`` and no business data.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def assert_tool_sequence(
    actual: Sequence[str],
    expected: Sequence[str],
) -> tuple[bool, dict[str, Any]]:
    """Check that ``expected`` tools appear in ``actual`` in order.

    Intermediate calls (for example schema discovery) are tolerated; only the
    relative order of the expected tools matters.
    """
    matched = 0
    for tool in actual:
        if matched < len(expected) and tool == expected[matched]:
            matched += 1
    passed = matched == len(expected)
    return passed, {
        "expected_tools": list(expected),
        "actual_tools": list(actual),
        "matched_in_order": matched,
    }


def assert_no_tools(
    actual: Sequence[str],
    forbidden: Sequence[str],
) -> tuple[bool, dict[str, Any]]:
    forbidden_set = set(forbidden)
    hits = [tool for tool in actual if tool in forbidden_set]
    return not hits, {
        "forbidden_tools": list(forbidden),
        "violations": hits,
    }


def assert_text_substrings(
    text: str,
    *,
    must_contain: Sequence[str] = (),
    must_contain_any: Sequence[str] = (),
    must_not_contain: Sequence[str] = (),
) -> tuple[bool, dict[str, Any]]:
    missing = [fragment for fragment in must_contain if fragment not in text]
    matched_any = [fragment for fragment in must_contain_any if fragment in text]
    violations = [fragment for fragment in must_not_contain if fragment in text]
    passed = (
        not missing
        and not violations
        and (not must_contain_any or bool(matched_any))
    )
    return passed, {
        "missing": missing,
        "matched_any": matched_any,
        "violations": violations,
    }


PERMISSION_DENIED_MARKERS: tuple[str, ...] = (
    "权限",
    "无权",
    "没有权限",
    "拒绝访问",
    "不允许",
    "无法访问",
    "permission",
    "denied",
    "PERMISSION_DENIED",
)


def looks_permission_denied(text: str) -> bool:
    return any(marker in text for marker in PERMISSION_DENIED_MARKERS)
