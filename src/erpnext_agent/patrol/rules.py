from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PatrolRuleKind(StrEnum):
    OVERDUE_RECEIVABLES = "overdue_receivables"
    STOCK_BALANCE_THRESHOLD = "stock_balance_threshold"
    DOCUMENT_STATUS_SCAN = "document_status_scan"
    METRIC_CHANGE_THRESHOLD = "metric_change_threshold"


@dataclass(frozen=True, slots=True)
class PatrolRule:
    name: str
    kind: PatrolRuleKind
    enabled: bool = True

