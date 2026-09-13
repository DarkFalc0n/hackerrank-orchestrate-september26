"""Adapt cash-forecaster recurring flows into the forecasting tool ledger."""

from __future__ import annotations

from datetime import timedelta
from typing import Sequence

from ...tools.forecasting import ForecastLedger
from .config import HORIZON_DAYS
from .models import RecurringFlow
from .projection import occurrences


def stream_id_for(flow: RecurringFlow) -> str:
    """Stable identifier for a recurring stream, used by stream-level tools."""
    return "stream::" + "|".join(
        [
            flow.user_id,
            flow.event_type,
            flow.category or "",
            flow.description or "",
            flow.direction,
        ]
    )


def recurring_flows_to_ledger(
    recurring_flows: Sequence[RecurringFlow],
    *,
    request_id: str,
    user_id: str,
    currency: str,
    opening_balance: float,
    start,
    horizon: int = HORIZON_DAYS,
) -> ForecastLedger:
    """Seed a :class:`ForecastLedger` with a request's recurring occurrences."""
    ledger = ForecastLedger(
        request_id=request_id,
        user_id=user_id,
        currency=currency,
        opening_balance=opening_balance,
        start=start,
        horizon=horizon,
    )
    end = start + timedelta(days=horizon)
    for flow in recurring_flows:
        stream_id = stream_id_for(flow)
        category = flow.category or flow.event_type
        for day in occurrences(flow, start, end):
            ledger.add_flow(
                direction=flow.direction,
                category=category,
                description=flow.description,
                amount=flow.amount,
                flow_date=day,
                event_type=flow.event_type,
                interval_days=flow.period_days,
                stream_id=stream_id,
            )
    return ledger


__all__ = ["recurring_flows_to_ledger", "stream_id_for"]
