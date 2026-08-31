"""Deterministic relative-time ranges injected per business request.

Models have no reliable clock: providers may or may not inject a date, and a
guess silently corrupts every "本月/上月/上季度/年初至今" query. The business
agents therefore receive an explicit, system-generated date context (the same
runtime-context pattern as warehouse abbreviation rules) computed from the
server clock, so relative time expressions resolve deterministically and the
answer can cite the exact range that was queried.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

_WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _month_bounds(day: date, offset: int = 0) -> tuple[date, date]:
    month = day.month + offset
    year = day.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _quarter_bounds(day: date) -> tuple[date, date]:
    start_month = (day.month - 1) // 3 * 3 + 1
    end_month = start_month + 2
    last = calendar.monthrange(day.year, end_month)[1]
    return date(day.year, start_month, 1), date(day.year, end_month, last)


def format_business_date_context(today: date | None = None) -> str:
    """Render the runtime date and the standard relative ranges in Chinese."""
    day = today or date.today()
    week_start = day - timedelta(days=day.weekday())
    week_end = week_start + timedelta(days=6)
    recent_start = day - timedelta(days=6)
    month_start, month_end = _month_bounds(day)
    prev_start, prev_end = _month_bounds(day, -1)
    quarter_start, quarter_end = _quarter_bounds(day)
    year_start = date(day.year, 1, 1)
    return (
        f"今天是 {day.isoformat()}（{_WEEKDAYS_CN[day.weekday()]}）。"
        f"本周（周一至周日）：{week_start.isoformat()} 至 {week_end.isoformat()}。\n"
        f"最近 7 天（含今天）：{recent_start.isoformat()} 至 {day.isoformat()}。\n"
        f"本月：{month_start.isoformat()} 至 {month_end.isoformat()}。"
        f"上月：{prev_start.isoformat()} 至 {prev_end.isoformat()}。\n"
        f"本季度：{quarter_start.isoformat()} 至 {quarter_end.isoformat()}。"
        f"年初至今：{year_start.isoformat()} 至 {day.isoformat()}。"
    )
