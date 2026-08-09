from __future__ import annotations

from decimal import Decimal


def ratio(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if whole == 0 else part / whole


def period_change(current: Decimal, previous: Decimal) -> Decimal | None:
    return None if previous == 0 else (current - previous) / previous

