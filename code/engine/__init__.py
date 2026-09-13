"""Deterministic engine: non-LLM financial reasoning.

The engine is decoupled from the request analyser, the message analyser, and the
LLMs. It resolves pending and scheduled cash events into a forecast ledger and
generates the candidate payment plans a request analyser may later choose from.
"""

from .payment_plans import PaymentPlanEngine, build_payment_plan_options
from .plan_models import (
    PLAN_OPTION_DTYPES,
    PaymentPlanOption,
    plan_options_frame,
)
from .resolve_events import (
    CASH_DIRECTIONS,
    PENDING_SCHEDULED_STATUSES,
    RESOLUTION_TOOL,
    filter_pending_scheduled_events,
    resolve_pending_scheduled_events,
)

__all__ = [
    "CASH_DIRECTIONS",
    "PENDING_SCHEDULED_STATUSES",
    "PLAN_OPTION_DTYPES",
    "RESOLUTION_TOOL",
    "PaymentPlanEngine",
    "PaymentPlanOption",
    "build_payment_plan_options",
    "filter_pending_scheduled_events",
    "plan_options_frame",
    "resolve_pending_scheduled_events",
]
