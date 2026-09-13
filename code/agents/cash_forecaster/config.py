"""Tunable constants for the cash forecaster agent."""

from __future__ import annotations

from ...ingest.models import Direction, EventStatus, Flexibility
from .models import Cadence

HORIZON_DAYS = 90

MIN_RECURRENCES = 3
MIN_CADENCE_CONSISTENCY = 0.6

RECURRING_FLEXIBILITIES: tuple[str, ...] = (
    Flexibility.fixed.value,
    Flexibility.reducible.value,
    Flexibility.stoppable.value,
    Flexibility.reducible_or_stoppable.value,
)
DETECTION_STATUSES: tuple[str, ...] = (EventStatus.settled.value,)
CASH_DIRECTIONS: tuple[str, ...] = (
    Direction.credit.value,
    Direction.debit.value,
)

CADENCE_WINDOWS: dict[Cadence, tuple[int, int]] = {
    Cadence.weekly: (6, 8),
    Cadence.biweekly: (13, 15),
    Cadence.monthly: (26, 32),
}

CADENCE_PERIOD_DAYS: dict[Cadence, int] = {
    Cadence.weekly: 7,
    Cadence.biweekly: 14,
    Cadence.monthly: 30,
}

__all__ = [
    "CADENCE_PERIOD_DAYS",
    "CADENCE_WINDOWS",
    "CASH_DIRECTIONS",
    "DETECTION_STATUSES",
    "HORIZON_DAYS",
    "MIN_CADENCE_CONSISTENCY",
    "MIN_RECURRENCES",
    "RECURRING_FLEXIBILITIES",
]
