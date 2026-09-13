"""Deterministic cash-forecasting agent."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

import polars as pl

from ...ingest.models import (
    Currency,
    Direction,
    EventCategory,
    EventStatus,
    EventType,
    Flexibility,
)
from ...ingest.pipeline import DEFAULT_DATASET_DIR, load_dataset
from ...ingest.store import InMemoryDataStore
from ...tools import CurrencyConverter
from ...tools.forecasting import ForecastLedger
from .bridge import recurring_flows_to_ledger
from .config import (
    CADENCE_PERIOD_DAYS,
    CASH_DIRECTIONS,
    DETECTION_STATUSES,
    HORIZON_DAYS,
    PROTECTED_SPEND_LOOKBACK_DAYS,
    RECURRING_FLEXIBILITIES,
    SALARY_TERMINATION_DESCRIPTION,
)
from .models import (
    Cadence,
    CashForecastResult,
    RecurringEvent,
    RecurringFlow,
    forecast_frame,
    recurring_events_frame,
    recurring_flows_frame,
)
from .projection import build_forecast, occurrences
from .recurrence import RecurrenceResult, detect_recurring

_NORMALIZED_EVENT_DTYPES: dict[str, Any] = {
    "event_id": pl.String,
    "user_id": pl.String,
    "event_type": pl.String,
    "category": pl.String,
    "description": pl.String,
    "direction": pl.String,
    "flexibility": pl.String,
    "status": pl.String,
    "event_date": pl.Date,
    "amount": pl.Float64,
}


def _category_tokens(value: str | None) -> set[str]:
    """Split a pipe-delimited profile category list into a set."""
    if not value:
        return set()
    return {token.strip() for token in value.split("|") if token.strip()}


def _series_key(
    user_id: str,
    event_type: str,
    category: str | None,
    description: str | None,
    direction: str,
) -> tuple[str, str, str, str, str]:
    return (user_id, event_type, category or "", description or "", direction)


class CashForecaster:
    """Project each request's usable cash balance over a 90-day horizon.

    The agent is deterministic: it detects weekly, biweekly, and monthly fixed,
    reducible, and stoppable cash flows for the requesting user, normalises
    foreign-currency amounts to the home currency using the dated exchange
    rates, and rolls the current available balance forward day by day.
    """

    def __init__(
        self,
        store: InMemoryDataStore | None = None,
        dataset_dir: Path | str | None = None,
    ) -> None:
        self._dataset_dir = (
            Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
        )
        self._store = store or load_dataset(self._dataset_dir)
        self._profiles = self._store.table("financial_profiles")
        self._events = self._store.table("financial_events")
        self._requests = self._store.table("requests")
        self._converter = CurrencyConverter.from_store(self._store)

    @property
    def store(self) -> InMemoryDataStore:
        return self._store

    def profile(self, user_id: str) -> dict[str, Any]:
        """Return the financial profile row for ``user_id``."""
        rows = self._profiles.filter(pl.col("user_id") == user_id)
        if rows.height == 0:
            raise KeyError(f"no financial profile for user {user_id!r}")
        return rows.row(0, named=True)

    def _home_amount(self, row: dict[str, Any], home_currency: str) -> float:
        amount = float(row["amount"])
        if row["currency"] == home_currency:
            return amount
        rate_date = row["settlement_date"] or row["event_date"]
        return self._converter.convert(
            amount, row["currency"], home_currency, rate_date
        )

    def detect_recurring_for_user(
        self,
        user_id: str,
        as_of: date,
        profile: dict[str, Any] | None = None,
    ) -> RecurrenceResult:
        """Detect recurring flows and member events for ``user_id``."""
        profile = profile or self.profile(user_id)
        home_currency = profile["home_currency"]

        events = self._events.filter(
            (pl.col("user_id") == user_id)
            & (pl.col("event_date") <= as_of)
            & pl.col("status").is_in(list(DETECTION_STATUSES))
            & pl.col("direction").is_in(list(CASH_DIRECTIONS))
            & pl.col("flexibility").is_in(list(RECURRING_FLEXIBILITIES))
            & pl.col("amount").is_not_null()
        )
        normalized = [
            {
                "event_id": row["event_id"],
                "user_id": row["user_id"],
                "event_type": row["event_type"],
                "category": row["category"],
                "description": row["description"],
                "direction": row["direction"],
                "flexibility": row["flexibility"],
                "status": row["status"],
                "event_date": row["event_date"],
                "amount": self._home_amount(row, home_currency),
            }
            for row in events.iter_rows(named=True)
        ]
        frame = pl.DataFrame(
            normalized, schema=_NORMALIZED_EVENT_DTYPES, orient="row"
        )
        detected = detect_recurring(frame, home_currency)
        self._add_confirmed_income(user_id, home_currency, detected)
        self._add_protected_category_spending(
            user_id, as_of, home_currency, profile, detected
        )
        self._apply_salary_termination(user_id, detected)
        return detected

    def _add_confirmed_income(
        self,
        user_id: str,
        home_currency: str,
        detected: RecurrenceResult,
    ) -> None:
        """Project the confirmed "next salary" as a monthly income stream.

        ``financial_events`` marks the next confirmed salary with a scheduled
        income/salary row. A user whose salary history is too short to establish
        a cadence (for example a first, prorated salary) still has that confirmed
        salary, so it anchors a monthly stream for the forecast horizon. When a
        settled history already produced a monthly salary stream on the same
        anchor day, the confirmed row is treated as its next occurrence instead
        of a second income stream.
        """
        scheduled = self._events.filter(
            (pl.col("user_id") == user_id)
            & (pl.col("status") == EventStatus.scheduled.value)
            & (pl.col("direction") == Direction.credit.value)
            & (pl.col("category") == EventCategory.salary.value)
            & pl.col("amount").is_not_null()
        )
        existing_anchors = [
            flow.anchor_day
            for flow in detected.flows
            if flow.direction == Direction.credit.value
            and flow.category == EventCategory.salary.value
            and flow.cadence == Cadence.monthly
            and flow.anchor_day is not None
        ]

        for row in scheduled.iter_rows(named=True):
            effective = row["settlement_date"] or row["event_date"]
            if effective is None:
                continue
            if any(
                abs(anchor - effective.day) <= 2 for anchor in existing_anchors
            ):
                continue
            amount = self._home_amount(row, home_currency)
            detected.flows.append(
                RecurringFlow(
                    user_id=user_id,
                    event_type=row["event_type"],
                    category=row["category"],
                    description=row["description"],
                    direction=row["direction"],
                    cadence=Cadence.monthly,
                    period_days=CADENCE_PERIOD_DAYS[Cadence.monthly],
                    amount=amount,
                    currency=Currency(home_currency),
                    occurrences=1,
                    first_date=effective,
                    last_date=effective,
                    anchor_day=effective.day,
                    flexibility=row["flexibility"],
                    last_event_id=row["event_id"],
                )
            )
            detected.events.append(
                RecurringEvent(
                    event_id=row["event_id"],
                    user_id=user_id,
                    event_type=row["event_type"],
                    category=row["category"],
                    description=row["description"],
                    direction=row["direction"],
                    flexibility=row["flexibility"],
                    event_date=effective,
                    amount=amount,
                    currency=Currency(home_currency),
                    cadence=Cadence.monthly.value,
                )
            )

    def _add_protected_category_spending(
        self,
        user_id: str,
        as_of: date,
        home_currency: str,
        profile: dict[str, Any],
        detected: RecurrenceResult,
    ) -> None:
        """Add the unmodelled average spend of protected categories.

        Recurrence detection only captures protected spending that repeats on a
        clean weekly/biweekly/monthly cadence. Essential categories such as
        groceries and transport are frequently irregular, so their spend would
        otherwise be missing from the projection. For each protected category
        this averages the trailing historical spend, subtracts the recurring
        amount already projected for that category (to avoid double counting),
        and adds the positive remainder as a weekly outflow across the horizon.

        Only protected categories are reserved. Reserving every category the
        user is unwilling to cut over-tightens the forecast for users with large
        irregular non-protected spend, so that remains out of scope.
        """
        protected = _category_tokens(
            profile["expense_categories_to_protect"]
        )
        if not protected:
            return

        recurring_monthly: dict[str, float] = {}
        for flow in detected.flows:
            if flow.direction != Direction.debit.value:
                continue
            if flow.category not in protected:
                continue
            period = CADENCE_PERIOD_DAYS[Cadence(flow.cadence)]
            recurring_monthly[flow.category] = recurring_monthly.get(
                flow.category, 0.0
            ) + flow.amount * (30.0 / period)

        since = as_of - timedelta(days=PROTECTED_SPEND_LOOKBACK_DAYS)
        history = self._events.filter(
            (pl.col("user_id") == user_id)
            & (pl.col("direction") == Direction.debit.value)
            & (pl.col("status") == EventStatus.settled.value)
            & (pl.col("event_date") <= as_of)
            & (pl.col("event_date") > since)
            & pl.col("category").is_in(sorted(protected))
            & pl.col("amount").is_not_null()
        )
        spend: dict[str, float] = {}
        for row in history.iter_rows(named=True):
            category = row["category"]
            spend[category] = spend.get(category, 0.0) + self._home_amount(
                row, home_currency
            )

        months = PROTECTED_SPEND_LOOKBACK_DAYS / 30.0
        weekly_days = CADENCE_PERIOD_DAYS[Cadence.weekly]
        for category in sorted(protected):
            residual = spend.get(category, 0.0) / months - recurring_monthly.get(
                category, 0.0
            )
            if residual <= 0:
                continue
            detected.flows.append(
                RecurringFlow(
                    user_id=user_id,
                    event_type=EventType.expense,
                    category=category,
                    description=f"Protected average spending: {category}",
                    direction=Direction.debit,
                    cadence=Cadence.weekly,
                    period_days=weekly_days,
                    amount=residual * (weekly_days / 30.0),
                    currency=Currency(home_currency),
                    occurrences=1,
                    first_date=as_of,
                    last_date=as_of,
                    flexibility=Flexibility.fixed,
                )
            )

    def _apply_salary_termination(
        self, user_id: str, detected: RecurrenceResult
    ) -> None:
        """Stop salary streams at the settled "Final employer payroll" event.

        The dataset marks the end of an employment with a settled salary credit
        whose description is :data:`SALARY_TERMINATION_DESCRIPTION`. It is the
        last paycheck, so the matching salary stream must not project past its
        date. The terminal occurrence itself is still kept by the occurrence
        generator (the cap is inclusive).
        """
        terminal = self._events.filter(
            (pl.col("user_id") == user_id)
            & (pl.col("description") == SALARY_TERMINATION_DESCRIPTION)
            & (pl.col("category") == EventCategory.salary.value)
            & (pl.col("direction") == Direction.credit.value)
            & pl.col("amount").is_not_null()
        )
        termination_dates = [
            row["settlement_date"] or row["event_date"]
            for row in terminal.iter_rows(named=True)
        ]
        termination_dates = [value for value in termination_dates if value is not None]
        if not termination_dates:
            return

        for flow in detected.flows:
            if (
                flow.direction != Direction.credit.value
                or flow.category != EventCategory.salary.value
            ):
                continue
            anchor = flow.anchor_day or flow.last_date.day
            matches = [
                value
                for value in termination_dates
                if value >= flow.last_date and abs(value.day - anchor) <= 2
            ]
            if matches:
                flow.termination_date = min(matches)

    def _stamp_first_forecast(
        self, detected: RecurrenceResult, start: date
    ) -> None:
        end = start + timedelta(days=HORIZON_DAYS)
        first_by_series: dict[tuple[str, str, str, str, str], date | None] = {}
        for flow in detected.flows:
            upcoming = occurrences(flow, start, end)
            flow.first_forecast_date = upcoming[0] if upcoming else None
            first_by_series[
                _series_key(
                    flow.user_id,
                    flow.event_type,
                    flow.category,
                    flow.description,
                    flow.direction,
                )
            ] = flow.first_forecast_date
        for event in detected.events:
            event.first_forecast_date = first_by_series.get(
                _series_key(
                    event.user_id,
                    event.event_type,
                    event.category,
                    event.description,
                    event.direction,
                )
            )

    def build_ledger(
        self, request_id: str, horizon: int = HORIZON_DAYS
    ) -> ForecastLedger:
        """Return a tool-ready ledger seeded with a request's recurring flows."""
        rows = self._requests.filter(pl.col("request_id") == request_id)
        if rows.height == 0:
            raise KeyError(f"no request {request_id!r}")
        request = rows.row(0, named=True)
        profile = self.profile(request["user_id"])
        detected = self.detect_recurring_for_user(
            request["user_id"], request["request_date"], profile
        )
        return recurring_flows_to_ledger(
            detected.flows,
            request_id=request_id,
            user_id=request["user_id"],
            currency=profile["home_currency"],
            opening_balance=profile["current_available_balance"],
            start=request["request_date"],
            horizon=horizon,
        )

    def run(self, request_ids: Sequence[str] | None = None) -> CashForecastResult:
        """Forecast every selected request into the ``forecast`` dataframe."""
        requests = self._requests
        if request_ids is not None:
            requests = requests.filter(
                pl.col("request_id").is_in(list(request_ids))
            )

        flow_records: list[RecurringFlow] = []
        event_records: list[RecurringEvent] = []
        entry_records = []
        for request in requests.iter_rows(named=True):
            profile = self.profile(request["user_id"])
            detected = self.detect_recurring_for_user(
                request["user_id"], request["request_date"], profile
            )
            self._stamp_first_forecast(detected, request["request_date"])
            entry_records.extend(
                build_forecast(
                    detected.flows,
                    profile["current_available_balance"],
                    request["request_date"],
                    request_id=request["request_id"],
                    user_id=request["user_id"],
                    currency=profile["home_currency"],
                )
            )
            flow_records.extend(detected.flows)
            event_records.extend(detected.events)

        return CashForecastResult(
            forecast=forecast_frame(entry_records),
            recurring_flows=recurring_flows_frame(flow_records),
            recurring_events=recurring_events_frame(event_records),
            store=self._store,
        )


__all__ = ["CashForecaster"]
