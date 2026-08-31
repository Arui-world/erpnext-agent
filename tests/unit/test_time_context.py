"""Offline tests for the deterministic runtime date context."""

from __future__ import annotations

from datetime import date

from erpnext_agent.agents.time_context import format_business_date_context


def test_typical_summer_saturday_ranges() -> None:
    text = format_business_date_context(date(2026, 8, 29))
    assert "今天是 2026-08-29（周六）" in text
    assert "本周（周一至周日）：2026-08-24 至 2026-08-30" in text
    assert "最近 7 天（含今天）：2026-08-23 至 2026-08-29" in text
    assert "本月：2026-08-01 至 2026-08-31" in text
    assert "上月：2026-07-01 至 2026-07-31" in text
    assert "本季度：2026-07-01 至 2026-09-30" in text
    assert "年初至今：2026-01-01 至 2026-08-29" in text


def test_january_rolls_previous_month_and_year() -> None:
    text = format_business_date_context(date(2026, 1, 15))
    assert "上月：2025-12-01 至 2025-12-31" in text
    assert "本季度：2026-01-01 至 2026-03-31" in text
    assert "年初至今：2026-01-01 至 2026-01-15" in text
    # 2026-01-15 is a Thursday.
    assert "（周四）" in text


def test_quarter_end_and_leap_february() -> None:
    text = format_business_date_context(date(2026, 12, 31))
    assert "本月：2026-12-01 至 2026-12-31" in text
    assert "本季度：2026-10-01 至 2026-12-31" in text
    assert "上月：2026-11-01 至 2026-11-30" in text
    leap = format_business_date_context(date(2028, 3, 10))
    assert "上月：2028-02-01 至 2028-02-29" in leap


def test_context_is_data_only() -> None:
    # The block is facts for the system prompt; usage rules live in the agent
    # prompts themselves, so this text must not carry imperative clauses.
    text = format_business_date_context(date(2026, 8, 29))
    assert "今天是" in text
    assert "不得" not in text
    assert "必须" not in text
