"""Calendar arithmetic and 90-day balance projection."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Mapping, Sequence

from .config import CADENCE_PERIOD_DAYS, HORIZON_DAYS
from .models import Cadence, ForecastEntry, RecurringFlow


def add_months(anchor: date, months: int) -> date:
    """Shift ``anchor`` by whole months, clamping the day to the month end."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, monthrange(year, month)[1])
    return date(year, month, day)


def advance(anchor: date, cadence: Cadence) -> date:
    """Return the next occurrence date for ``cadence``."""
    if cadence == Cadence.monthly:
        return add_months(anchor, 1)
    return anchor + timedelta(days=CADENCE_PERIOD_DAYS[cadence])


def retreat(anchor: date, cadence: Cadence) -> date:
    """Return the previous occurrence date for ``cadence``."""
    if cadence == Cadence.monthly:
        return add_months(anchor, -1)
    return anchor - timedelta(days=CADENCE_PERIOD_DAYS[cadence])


def occurrences(flow: RecurringFlow, start: date, end: date) -> list[date]:
    """Return the flow's occurrence dates in ``[start, end]``.

    Weekly and biweekly flows are stepped by a fixed number of days. Monthly
    flows are anchored on the flow's ``anchor_day`` so month-end clamping (for
    example the 31st) does not drift across months.
    """
    if end < start:
        return []
    cadence = Cadence(flow.cadence)
    if cadence == Cadence.monthly:
        return _monthly_occurrences(flow, start, end)

    found: set[date] = set()
    cursor = flow.last_date
    while cursor <= end:
        if cursor >= start:
            found.add(cursor)
        cursor = advance(cursor, cadence)

    cursor = retreat(flow.last_date, cadence)
    while cursor >= start:
        if cursor <= end:
            found.add(cursor)
        cursor = retreat(cursor, cadence)

    return sorted(found)


def _month_occurrence(anchor: date, months: int, anchor_day: int) -> date:
    shifted = add_months(anchor, months)
    day = min(anchor_day, monthrange(shifted.year, shifted.month)[1])
    return date(shifted.year, shifted.month, day)


def _monthly_occurrences(
    flow: RecurringFlow, start: date, end: date
) -> list[date]:
    anchor_day = flow.anchor_day or flow.last_date.day
    found: set[date] = set()

    offset = 0
    while True:
        current = _month_occurrence(flow.last_date, offset, anchor_day)
        if current > end:
            break
        if current >= start:
            found.add(current)
        offset += 1

    offset = -1
    while True:
        current = _month_occurrence(flow.last_date, offset, anchor_day)
        if current < start:
            break
        if current <= end:
            found.add(current)
        offset -= 1

    return sorted(found)


def _schedule(
    flows: Sequence[RecurringFlow], start: date, end: date
) -> Mapping[date, list[RecurringFlow]]:
    schedule: dict[date, list[RecurringFlow]] = {}
    for flow in flows:
        for day in occurrences(flow, start, end):
            schedule.setdefault(day, []).append(flow)
    return schedule


def build_forecast(
    flows: Sequence[RecurringFlow],
    opening_balance: float,
    start: date,
    *,
    request_id: str,
    user_id: str,
    currency: str,
    horizon: int = HORIZON_DAYS,
) -> list[ForecastEntry]:
    """Project a daily balance for ``horizon`` days from ``start``.

    Day 0 closes at ``opening_balance``; recurring flows due on day 0 are not
    applied because the current balance already reflects them.
    """
    end = start + timedelta(days=horizon)
    schedule = _schedule(flows, start, end)

    entries: list[ForecastEntry] = []
    balance = float(opening_balance)
    for day_index in range(horizon + 1):
        current = start + timedelta(days=day_index)
        todays = schedule.get(current, []) if day_index > 0 else []
        inflow = sum(flow.amount for flow in todays if flow.direction == "credit")
        outflow = sum(flow.amount for flow in todays if flow.direction == "debit")
        balance += inflow - outflow
        entries.append(
            ForecastEntry(
                request_id=request_id,
                user_id=user_id,
                currency=currency,
                forecast_date=current,
                day_index=day_index,
                inflow=inflow,
                outflow=outflow,
                net_flow=inflow - outflow,
                closing_balance=balance,
            )
        )
    return entries


__all__ = [
    "add_months",
    "advance",
    "build_forecast",
    "occurrences",
    "retreat",
]
