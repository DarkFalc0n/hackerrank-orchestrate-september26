"""Deterministic detection of recurring cash flows.

A group of same-signed events (direction, event type, category, description) is
declared recurring when it has at least ``MIN_RECURRENCES`` settled occurrences,
its median gap falls inside a supported cadence window, and enough of its gaps
fall inside that window.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from statistics import median

import polars as pl

from ...ingest.models import EventStatus
from .config import (
    CADENCE_PERIOD_DAYS,
    CADENCE_WINDOWS,
    MIN_CADENCE_CONSISTENCY,
    MIN_RECURRENCES,
)
from .models import Cadence, RecurringEvent, RecurringFlow

GROUP_KEYS: tuple[str, ...] = (
    "event_type",
    "category",
    "description",
    "direction",
)

REQUIRED_COLUMNS: tuple[str, ...] = (
    "event_id",
    "user_id",
    *GROUP_KEYS,
    "flexibility",
    "status",
    "event_date",
    "amount",
)


@dataclass
class RecurrenceResult:
    """Detected recurring flows plus the events that compose them."""

    flows: list[RecurringFlow] = field(default_factory=list)
    events: list[RecurringEvent] = field(default_factory=list)


def _representative_day(dates: list[date]) -> int:
    """Return the most common day-of-month, breaking ties toward the earliest."""
    counts = Counter(day.day for day in dates)
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def classify_cadence(median_days: float) -> Cadence | None:
    """Return the cadence whose window contains ``median_days``, if any."""
    for cadence, (low, high) in CADENCE_WINDOWS.items():
        if low <= median_days <= high:
            return cadence
    return None


def detect_recurring(events: pl.DataFrame, currency: str) -> RecurrenceResult:
    """Detect recurring flows and their member events.

    ``events`` must contain the columns in :data:`REQUIRED_COLUMNS`, with
    ``amount`` already normalised to ``currency`` and no null amounts. Only rows
    whose ``status`` is ``settled`` can seed a flow; any other status is ignored
    even if the caller passed it in.
    """
    missing = set(REQUIRED_COLUMNS) - set(events.columns)
    if missing:
        raise ValueError(f"events missing columns: {sorted(missing)}")
    result = RecurrenceResult()
    events = events.filter(pl.col("status") == EventStatus.settled.value)
    if events.height == 0:
        return result

    ordered = events.sort([*GROUP_KEYS, "event_date"])
    for keys, group in ordered.group_by(
        ["user_id", *GROUP_KEYS], maintain_order=True
    ):
        user_id, event_type, category, description, direction = keys
        detected = _build_flow(
            user_id=str(user_id),
            event_type=str(event_type),
            category=category,
            description=description,
            direction=str(direction),
            group=group,
            currency=currency,
        )
        if detected is None:
            continue
        flow, members = detected
        result.flows.append(flow)
        result.events.extend(members)

    result.flows.sort(key=_flow_sort_key)
    result.events.sort(
        key=lambda event: (event.user_id, event.event_date, event.event_id)
    )
    return result


def detect_recurring_flows(
    events: pl.DataFrame, currency: str
) -> list[RecurringFlow]:
    """Convenience wrapper returning only the detected recurring flows."""
    return detect_recurring(events, currency).flows


def _flow_sort_key(flow: RecurringFlow) -> tuple[str, str, str, str, str]:
    return (
        flow.user_id,
        flow.direction,
        flow.category or "",
        flow.description or "",
        flow.event_type,
    )


def _build_flow(
    *,
    user_id: str,
    event_type: str,
    category: str | None,
    description: str | None,
    direction: str,
    group: pl.DataFrame,
    currency: str,
) -> tuple[RecurringFlow, list[RecurringEvent]] | None:
    dates = group["event_date"].to_list()
    if len(dates) < MIN_RECURRENCES:
        return None

    intervals = [
        (dates[index + 1] - dates[index]).days for index in range(len(dates) - 1)
    ]
    intervals = [gap for gap in intervals if gap > 0]
    if len(intervals) < MIN_RECURRENCES - 1:
        return None

    cadence = classify_cadence(median(intervals))
    if cadence is None:
        return None

    low, high = CADENCE_WINDOWS[cadence]
    in_window = sum(1 for gap in intervals if low <= gap <= high)
    if in_window / len(intervals) < MIN_CADENCE_CONSISTENCY:
        return None

    amounts = [float(value) for value in group["amount"].to_list() if value is not None]
    if not amounts:
        return None

    flexibility = Counter(group["flexibility"].to_list()).most_common(1)[0][0]
    flow = RecurringFlow(
        user_id=user_id,
        event_type=event_type,
        category=category,
        description=description,
        direction=direction,
        cadence=cadence,
        period_days=CADENCE_PERIOD_DAYS[cadence],
        amount=float(median(amounts)),
        currency=currency,
        occurrences=len(dates),
        first_date=dates[0],
        last_date=dates[-1],
        anchor_day=(
            _representative_day(dates) if cadence == Cadence.monthly else None
        ),
        flexibility=flexibility,
    )
    members = [
        RecurringEvent(
            event_id=row["event_id"],
            user_id=user_id,
            event_type=event_type,
            category=category,
            description=description,
            direction=direction,
            flexibility=row["flexibility"],
            event_date=row["event_date"],
            amount=float(row["amount"]),
            currency=currency,
            cadence=cadence.value,
        )
        for row in group.iter_rows(named=True)
    ]
    return flow, members


__all__ = [
    "GROUP_KEYS",
    "REQUIRED_COLUMNS",
    "RecurrenceResult",
    "classify_cadence",
    "detect_recurring",
    "detect_recurring_flows",
]
