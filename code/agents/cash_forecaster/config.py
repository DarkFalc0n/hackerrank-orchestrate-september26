"""Tunable constants for the cash forecaster agent."""

from __future__ import annotations

from ...ingest.models import Direction, EventStatus, Flexibility
from .models import Cadence

HORIZON_DAYS = 90

MIN_RECURRENCES = 3
MIN_CADENCE_CONSISTENCY = 0.6

# Trailing window used to average a protected category's historical spending so
# essential but irregular spending (for example groceries or transport) is
# reflected consistently in the forecast even when it forms no clean cadence.
PROTECTED_SPEND_LOOKBACK_DAYS = 90

# A credit stream whose occurrence amounts never settle on a stable value (for
# example commissions, platform payouts, or a variable second household income)
# is not confirmed income. When this share of its occurrences carry a distinct
# amount, the stream is dropped rather than projected at its median, so the
# forecast never relies on money that may not arrive.
VARIABLE_INCOME_DISTINCT_RATIO = 0.75

# Exact description of the settled salary event that ends an employment: it is
# the last paycheck, so no salary may be projected after its date.
SALARY_TERMINATION_DESCRIPTION = "Final employer payroll"

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
    "PROTECTED_SPEND_LOOKBACK_DAYS",
    "RECURRING_FLEXIBILITIES",
    "SALARY_TERMINATION_DESCRIPTION",
    "VARIABLE_INCOME_DISTINCT_RATIO",
]
