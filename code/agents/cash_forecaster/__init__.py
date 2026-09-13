"""Cash forecaster agent: deterministic 90-day balance projections."""

from .agent import CashForecaster
from .models import (
    Cadence,
    CashForecastResult,
    ForecastEntry,
    RecurringEvent,
    RecurringFlow,
    forecast_frame,
    recurring_events_frame,
    recurring_flows_frame,
)
from .projection import build_forecast, occurrences
from .recurrence import (
    RecurrenceResult,
    detect_recurring,
    detect_recurring_flows,
)

__all__ = [
    "Cadence",
    "CashForecastResult",
    "CashForecaster",
    "ForecastEntry",
    "RecurrenceResult",
    "RecurringEvent",
    "RecurringFlow",
    "build_forecast",
    "detect_recurring",
    "detect_recurring_flows",
    "forecast_frame",
    "occurrences",
    "recurring_events_frame",
    "recurring_flows_frame",
]
