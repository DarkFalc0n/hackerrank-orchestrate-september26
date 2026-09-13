"""Pydantic models and dataframe schemas for the cash forecaster agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from ...ingest.models import (
    Currency,
    Direction,
    EventCategory,
    EventType,
    Flexibility,
)
from ...ingest.store import InMemoryDataStore


class Cadence(str, Enum):
    """Supported recurrence cadences."""

    weekly = "weekly"
    biweekly = "biweekly"
    monthly = "monthly"


class RecurringFlow(BaseModel):
    """A recurring cash flow detected for a user."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    user_id: str
    event_type: EventType
    category: EventCategory | None = None
    description: str | None = None
    direction: Direction
    cadence: Cadence
    period_days: int = Field(gt=0)
    amount: float = Field(ge=0)
    currency: Currency
    occurrences: int = Field(ge=1)
    first_date: date
    last_date: date
    anchor_day: int | None = Field(default=None, ge=1, le=31)
    flexibility: Flexibility | None = None
    first_forecast_date: date | None = None


class RecurringEvent(BaseModel):
    """One settled financial event that belongs to a detected recurring flow."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    event_id: str
    user_id: str
    event_type: EventType
    category: EventCategory | None = None
    description: str | None = None
    direction: Direction
    flexibility: Flexibility | None = None
    event_date: date
    amount: float = Field(ge=0)
    currency: Currency
    cadence: str
    first_forecast_date: date | None = None


class ForecastEntry(BaseModel):
    """One daily row of a request's 90-day balance projection."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    user_id: str
    currency: str
    forecast_date: date
    day_index: int = Field(ge=0)
    inflow: float = 0.0
    outflow: float = 0.0
    net_flow: float = 0.0
    closing_balance: float


RECURRING_FLOW_DTYPES: dict[str, Any] = {
    "user_id": pl.String,
    "event_type": pl.String,
    "category": pl.String,
    "description": pl.String,
    "direction": pl.String,
    "cadence": pl.String,
    "period_days": pl.Int64,
    "amount": pl.Float64,
    "currency": pl.String,
    "occurrences": pl.Int64,
    "first_date": pl.Date,
    "last_date": pl.Date,
    "anchor_day": pl.Int64,
    "flexibility": pl.String,
    "first_forecast_date": pl.Date,
}

RECURRING_EVENT_DTYPES: dict[str, Any] = {
    "event_id": pl.String,
    "user_id": pl.String,
    "event_type": pl.String,
    "category": pl.String,
    "description": pl.String,
    "direction": pl.String,
    "flexibility": pl.String,
    "event_date": pl.Date,
    "amount": pl.Float64,
    "currency": pl.String,
    "cadence": pl.String,
    "first_forecast_date": pl.Date,
}

FORECAST_DTYPES: dict[str, Any] = {
    "request_id": pl.String,
    "user_id": pl.String,
    "currency": pl.String,
    "forecast_date": pl.Date,
    "day_index": pl.Int64,
    "inflow": pl.Float64,
    "outflow": pl.Float64,
    "net_flow": pl.Float64,
    "closing_balance": pl.Float64,
}


def recurring_flows_frame(flows: list[RecurringFlow]) -> pl.DataFrame:
    """Build the typed ``recurring_flows`` dataframe."""
    return pl.DataFrame(
        [flow.model_dump() for flow in flows],
        schema=RECURRING_FLOW_DTYPES,
        orient="row",
    )


def recurring_events_frame(events: list[RecurringEvent]) -> pl.DataFrame:
    """Build the typed ``recurring_events`` dataframe."""
    return pl.DataFrame(
        [event.model_dump() for event in events],
        schema=RECURRING_EVENT_DTYPES,
        orient="row",
    )


def forecast_frame(entries: list[ForecastEntry]) -> pl.DataFrame:
    """Build the typed ``forecast`` dataframe."""
    return pl.DataFrame(
        [entry.model_dump() for entry in entries],
        schema=FORECAST_DTYPES,
        orient="row",
    )


@dataclass
class CashForecastResult:
    """Outcome of a cash-forecaster run."""

    forecast: pl.DataFrame
    recurring_flows: pl.DataFrame
    recurring_events: pl.DataFrame
    store: InMemoryDataStore


__all__ = [
    "FORECAST_DTYPES",
    "RECURRING_EVENT_DTYPES",
    "RECURRING_FLOW_DTYPES",
    "Cadence",
    "CashForecastResult",
    "ForecastEntry",
    "RecurringEvent",
    "RecurringFlow",
    "forecast_frame",
    "recurring_events_frame",
    "recurring_flows_frame",
]
