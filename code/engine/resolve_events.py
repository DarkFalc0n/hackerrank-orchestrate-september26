"""Deterministic resolution of pending and scheduled financial events.

This module is decoupled from the request analyser and the message analyser. It
takes the validated ``financial_events`` frame and a forecast ledger, then calls
the ``schedule_receivable_or_payable`` tool for every cash event that still needs
deterministic resolution:

* belongs to the ledger's user,
* has ``status`` ``scheduled`` or ``pending``,
* has a cash ``direction`` (``credit`` or ``debit``),
* has a non-null amount, and
* is not referenced by any ``messages.related_event_id`` (those are owned by the
  message analyser).

Every candidate row is re-validated with the shared
:class:`code.ingest.models.FinancialEvent` schema, and the status, direction, and
currency vocabulary comes from the ingest enums, so the engine cannot drift from
the ingestion contract. Amounts are converted to the ledger currency using the
dated exchange rates. Events already projected by a detected recurring stream on
the same date are skipped to avoid double counting. The function mutates the
ledger and returns the number of tool calls applied; it does not build or return
a dataframe.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from ..ingest.models import (
    Currency,
    Direction,
    EventStatus,
    FinancialEvent,
)
from ..tools import ConversionRateError, CurrencyConverter
from ..tools.forecasting import (
    FlowDirection,
    ForecastLedger,
    ScheduleReceivableOrPayableParams,
    ToolApplicationError,
    ToolName,
)

RESOLUTION_TOOL = ToolName.schedule_receivable_or_payable

# Vocabulary taken from the ingestion enums, never from bare strings.
PENDING_SCHEDULED_STATUSES: tuple[str, ...] = (
    EventStatus.scheduled.value,
    EventStatus.pending.value,
)
CASH_DIRECTIONS: tuple[str, ...] = (
    Direction.credit.value,
    Direction.debit.value,
)


def filter_pending_scheduled_events(
    events: pl.DataFrame, user_id: str | None = None
) -> pl.DataFrame:
    """Return the resolvable cash events, optionally for one user.

    Only rows with a ``scheduled``/``pending`` status, a cash direction, and a
    non-null amount are kept. The frame is a working set, not a result artifact.
    """
    predicate = (
        pl.col("status").is_in(list(PENDING_SCHEDULED_STATUSES))
        & pl.col("direction").is_in(list(CASH_DIRECTIONS))
        & pl.col("amount").is_not_null()
    )
    if user_id is not None:
        predicate = predicate & (pl.col("user_id") == user_id)
    return events.filter(predicate).sort(
        ["settlement_date", "event_date", "event_id"]
    )


def _message_linked_event_ids(messages: pl.DataFrame | None) -> set[str]:
    if messages is None:
        return set()
    return {
        str(value)
        for value in messages["related_event_id"].drop_nulls().to_list()
    }


def _effective_date(event: FinancialEvent) -> date:
    return event.settlement_date or event.event_date


def _already_projected(
    ledger: ForecastLedger,
    effective: date,
    direction: str,
    category: str,
    description: str | None,
    amount: float,
) -> bool:
    """True when a detected stream already projects this event on the date."""
    for flow in ledger.flows:
        if flow.stream_id is None or flow.date != effective:
            continue
        if flow.direction.value != direction:
            continue
        if description is not None and flow.description == description:
            return True
        if (
            description is None
            and flow.category == category
            and abs(flow.amount - amount) < 1e-6
        ):
            return True
    return False


def resolve_pending_scheduled_events(
    events: pl.DataFrame,
    ledger: ForecastLedger,
    *,
    messages: pl.DataFrame | None = None,
    converter: CurrencyConverter | None = None,
) -> int:
    """Apply the scheduling tool for one ledger's pending/scheduled events.

    Returns the number of events inserted into ``ledger``.
    """
    candidates = filter_pending_scheduled_events(events, user_id=ledger.user_id)
    linked = _message_linked_event_ids(messages)
    ledger_currency = Currency(ledger.currency).value
    applied = 0

    for row in candidates.iter_rows(named=True):
        event = FinancialEvent.model_validate(row)

        if event.event_id in linked:
            continue
        if event.status == EventStatus.pending.value and (
            event.direction == Direction.credit.value
        ):
            # Unconfirmed income must never be counted until it settles.
            continue
        if event.amount is None:
            continue

        effective = _effective_date(event)
        try:
            amount = float(event.amount)
            if event.currency != ledger_currency:
                if converter is None:
                    continue
                amount = converter.convert(
                    amount, event.currency, ledger_currency, effective
                )
        except (ConversionRateError, ValueError):
            continue

        category = event.category or event.event_type
        if _already_projected(
            ledger,
            effective,
            event.direction,
            category,
            event.description,
            amount,
        ):
            continue

        params = ScheduleReceivableOrPayableParams(
            direction=FlowDirection(event.direction.value),
            category=category,
            amount=amount,
            currency=Currency(ledger_currency),
            due_or_settlement_date=effective,
            is_recurring=False,
        )
        try:
            ledger.apply(RESOLUTION_TOOL, params)
        except ToolApplicationError:
            continue
        applied += 1

    return applied


__all__ = [
    "CASH_DIRECTIONS",
    "PENDING_SCHEDULED_STATUSES",
    "RESOLUTION_TOOL",
    "filter_pending_scheduled_events",
    "resolve_pending_scheduled_events",
]
