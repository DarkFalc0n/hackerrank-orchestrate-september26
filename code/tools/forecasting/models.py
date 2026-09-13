"""Pydantic schemas for the forecasting tool layer.

Each tool call is a validated Pydantic model. The ledger consumes these models
and rewrites an event-level set of scheduled cash flows, which is then projected
into an enriched daily 90-day forecast (including liquidity holds).
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...ingest.models import Currency, Direction, EventStatus, Flexibility


class FlowDirection(str, Enum):
    credit = Direction.credit.value
    debit = Direction.debit.value


class AdjustmentMode(str, Enum):
    set_fixed_amount = "set_fixed_amount"
    percentage_multiplier = "percentage_multiplier"
    terminate_stream = "terminate_stream"


class OneOffAdjustmentType(str, Enum):
    override_instance_amount = "override_instance_amount"
    add_lump_sum_credit = "add_lump_sum_credit"
    deduct_lump_sum = "deduct_lump_sum"


class PendingAction(str, Enum):
    confirm_settled = "confirm_settled"
    update_final_amount = "update_final_amount"
    hold_under_dispute = "hold_under_dispute"
    cancel_event = "cancel_event"


class InformationalKind(str, Enum):
    unrealized_market_valuation = "unrealized_market_valuation"
    unapproved_variable_income = "unapproved_variable_income"
    unverified_contingent_claim = "unverified_contingent_claim"


class FlowStatus(str, Enum):
    scheduled = EventStatus.scheduled.value
    pending = EventStatus.pending.value
    settled = EventStatus.settled.value
    disputed = "disputed"
    internal = "internal"
    cancelled = EventStatus.cancelled.value


class ToolName(str, Enum):
    update_recurring_stream = "update_recurring_stream"
    apply_one_off_adjustment = "apply_one_off_adjustment"
    reschedule_transaction_date = "reschedule_transaction_date"
    schedule_receivable_or_payable = "schedule_receivable_or_payable"
    resolve_pending_transaction = "resolve_pending_transaction"
    flag_internal_transfer = "flag_internal_transfer"
    apply_liquidity_hold = "apply_liquidity_hold"
    mark_informational_or_unconfirmed = "mark_informational_or_unconfirmed"


_BASE_CONFIG = ConfigDict(extra="forbid")


class UpdateRecurringStreamParams(BaseModel):
    """Tool 1: modify or terminate one specific recurring stream."""

    model_config = _BASE_CONFIG

    stream_id: str = Field(min_length=1)
    effective_date: date
    adjustment_mode: AdjustmentMode
    value: float = Field(ge=0)


class ApplyOneOffAdjustmentParams(BaseModel):
    """Tool 2: modify a single schedule instance or add a one-off entry."""

    model_config = _BASE_CONFIG

    target_category: str = Field(min_length=1)
    target_date: date
    adjustment_type: OneOffAdjustmentType
    amount: float = Field(ge=0)


class RescheduleTransactionDateParams(BaseModel):
    """Tool 3: shift a scheduled transaction's settlement date."""

    model_config = _BASE_CONFIG

    related_event_id: str | None = None
    category: str = Field(min_length=1)
    original_date: date
    new_settlement_date: date


class ScheduleReceivableOrPayableParams(BaseModel):
    """Tool 4: insert a new confirmed future cash flow."""

    model_config = _BASE_CONFIG

    direction: FlowDirection
    category: str = Field(min_length=1)
    amount: float = Field(ge=0)
    currency: Currency
    due_or_settlement_date: date
    is_recurring: bool = False
    recurrence_interval_days: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _require_interval(self) -> "ScheduleReceivableOrPayableParams":
        if self.is_recurring and self.recurrence_interval_days is None:
            raise ValueError(
                "recurrence_interval_days is required when is_recurring is true"
            )
        return self


class ResolvePendingTransactionParams(BaseModel):
    """Tool 5: resolve a pending/disputed transaction."""

    model_config = _BASE_CONFIG

    related_event_id: str = Field(min_length=1)
    action: PendingAction
    settled_amount: float | None = Field(default=None, ge=0)
    expected_settlement_days: int = Field(default=10, ge=0)


class FlagInternalTransferParams(BaseModel):
    """Tool 6: neutralise a dual-entry internal transfer."""

    model_config = _BASE_CONFIG

    transaction_ref: str = Field(min_length=1)
    neutralize_cash_flow: bool = True


class ApplyLiquidityHoldParams(BaseModel):
    """Tool 7: restrict spendable balance until a hold is released."""

    model_config = _BASE_CONFIG

    hold_amount: float = Field(gt=0)
    release_date: date | None = None
    reason: str = Field(min_length=1)


class MarkInformationalOrUnconfirmedParams(BaseModel):
    """Tool 8: log and discard a non-cash or unconfirmed notification."""

    model_config = _BASE_CONFIG

    event_type: InformationalKind
    exclude_from_cash_forecast: bool = True
    notes: str = Field(min_length=1)


TOOL_PARAMS: dict[ToolName, type[BaseModel]] = {
    ToolName.update_recurring_stream: UpdateRecurringStreamParams,
    ToolName.apply_one_off_adjustment: ApplyOneOffAdjustmentParams,
    ToolName.reschedule_transaction_date: RescheduleTransactionDateParams,
    ToolName.schedule_receivable_or_payable: ScheduleReceivableOrPayableParams,
    ToolName.resolve_pending_transaction: ResolvePendingTransactionParams,
    ToolName.flag_internal_transfer: FlagInternalTransferParams,
    ToolName.apply_liquidity_hold: ApplyLiquidityHoldParams,
    ToolName.mark_informational_or_unconfirmed: MarkInformationalOrUnconfirmedParams,
}


class ScheduledFlow(BaseModel):
    """One concrete cash-flow occurrence inside the forecast horizon."""

    model_config = _BASE_CONFIG

    flow_id: str
    user_id: str
    direction: FlowDirection
    category: str
    description: str | None = None
    amount: float = Field(ge=0)
    currency: Currency
    date: date
    event_type: str | None = None
    flexibility: Flexibility | None = None
    interval_days: int | None = Field(default=None, gt=0)
    stream_id: str | None = None
    event_id: str | None = None
    internal_ref: str | None = None
    status: FlowStatus = FlowStatus.scheduled


class LiquidityHold(BaseModel):
    """A non-cash restriction on the spendable balance."""

    model_config = _BASE_CONFIG

    hold_id: str
    user_id: str
    amount: float = Field(gt=0)
    start_date: date
    release_date: date | None = None
    reason: str


class InformationalNotice(BaseModel):
    """A logged non-cash or unconfirmed notification with no forecast impact."""

    model_config = _BASE_CONFIG

    notice_id: str
    user_id: str
    event_type: InformationalKind
    exclude_from_cash_forecast: bool = True
    notes: str
    logged_on: date


class ForecastRow(BaseModel):
    """One enriched daily row of a request's 90-day forecast."""

    model_config = _BASE_CONFIG

    request_id: str
    user_id: str
    currency: str
    forecast_date: date
    day_index: int = Field(ge=0)
    inflow: float = 0.0
    outflow: float = 0.0
    net_flow: float = 0.0
    closing_balance: float
    hold_amount: float = 0.0
    available_balance: float


FORECAST_ROW_DTYPES: dict[str, Any] = {
    "request_id": pl.String,
    "user_id": pl.String,
    "currency": pl.String,
    "forecast_date": pl.Date,
    "day_index": pl.Int64,
    "inflow": pl.Float64,
    "outflow": pl.Float64,
    "net_flow": pl.Float64,
    "closing_balance": pl.Float64,
    "hold_amount": pl.Float64,
    "available_balance": pl.Float64,
}


def forecast_frame(rows: list[ForecastRow]) -> pl.DataFrame:
    """Build the typed enriched forecast dataframe."""
    return pl.DataFrame(
        [row.model_dump() for row in rows],
        schema=FORECAST_ROW_DTYPES,
        orient="row",
    )


__all__ = [
    "FORECAST_ROW_DTYPES",
    "AdjustmentMode",
    "ApplyLiquidityHoldParams",
    "ApplyOneOffAdjustmentParams",
    "FlagInternalTransferParams",
    "FlowDirection",
    "FlowStatus",
    "ForecastRow",
    "InformationalKind",
    "InformationalNotice",
    "LiquidityHold",
    "MarkInformationalOrUnconfirmedParams",
    "OneOffAdjustmentType",
    "PendingAction",
    "RescheduleTransactionDateParams",
    "ResolvePendingTransactionParams",
    "ScheduleReceivableOrPayableParams",
    "ScheduledFlow",
    "TOOL_PARAMS",
    "ToolName",
    "UpdateRecurringStreamParams",
    "forecast_frame",
]
