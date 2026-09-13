"""Event-level forecast ledger and the seven forecasting tools.

The ledger holds concrete scheduled cash-flow occurrences plus liquidity holds.
Each tool call mutates the ledger deterministically; :meth:`ForecastLedger.forecast`
then rebuilds the enriched daily 90-day forecast dataframe.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

import polars as pl

from .models import (
    AdjustmentMode,
    ApplyLiquidityHoldParams,
    ApplyOneOffAdjustmentParams,
    Currency,
    FlagInternalTransferParams,
    Flexibility,
    FlowDirection,
    FlowStatus,
    ForecastRow,
    InformationalNotice,
    LiquidityHold,
    MarkInformationalOrUnconfirmedParams,
    OneOffAdjustmentType,
    PendingAction,
    RescheduleTransactionDateParams,
    ResolvePendingTransactionParams,
    ScheduleReceivableOrPayableParams,
    ScheduledFlow,
    TOOL_PARAMS,
    ToolName,
    UpdateRecurringStreamParams,
    forecast_frame,
)

DEFAULT_HORIZON_DAYS = 90


class ToolApplicationError(RuntimeError):
    """Raised when a tool call cannot be applied to the ledger."""


class ForecastLedger:
    """Deterministic event ledger for a single request's 90-day forecast."""

    def __init__(
        self,
        *,
        request_id: str,
        user_id: str,
        currency: str,
        opening_balance: float,
        start: date,
        horizon: int = DEFAULT_HORIZON_DAYS,
        flows: Sequence[ScheduledFlow] | None = None,
        holds: Sequence[LiquidityHold] | None = None,
    ) -> None:
        if horizon < 0:
            raise ValueError("horizon must be non-negative")
        self.request_id = request_id
        self.user_id = user_id
        self.currency = currency
        self.opening_balance = float(opening_balance)
        self.start = start
        self.horizon = horizon
        self._flows: list[ScheduledFlow] = list(flows or [])
        self._holds: list[LiquidityHold] = list(holds or [])
        self._notices: list[InformationalNotice] = []
        self._sequence = 0

    @property
    def end(self) -> date:
        """Last day covered by the forecast."""
        return self.start + timedelta(days=self.horizon)

    @property
    def flows(self) -> list[ScheduledFlow]:
        return list(self._flows)

    @property
    def holds(self) -> list[LiquidityHold]:
        return list(self._holds)

    @property
    def notices(self) -> list[InformationalNotice]:
        return list(self._notices)

    def _new_id(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}_{self._sequence:04d}"

    def add_flow(
        self,
        *,
        direction: FlowDirection | str,
        category: str,
        amount: float,
        flow_date: date,
        currency: str | None = None,
        description: str | None = None,
        event_type: str | None = None,
        flexibility: Flexibility | str | None = None,
        interval_days: int | None = None,
        stream_id: str | None = None,
        event_id: str | None = None,
        internal_ref: str | None = None,
        status: FlowStatus = FlowStatus.scheduled,
    ) -> ScheduledFlow:
        """Insert one concrete occurrence into the ledger."""
        flow = ScheduledFlow(
            flow_id=self._new_id("flow"),
            user_id=self.user_id,
            direction=FlowDirection(direction),
            category=category,
            description=description,
            amount=float(amount),
            currency=Currency(currency or self.currency),
            date=flow_date,
            event_type=event_type,
            flexibility=flexibility,
            interval_days=interval_days,
            stream_id=stream_id,
            event_id=event_id,
            internal_ref=internal_ref,
            status=status,
        )
        self._flows.append(flow)
        return flow

    # ------------------------------------------------------------------
    # Tool 1: update_recurring_stream
    # ------------------------------------------------------------------
    def update_recurring_stream(self, params: UpdateRecurringStreamParams) -> int:
        """Set, scale, or terminate exactly the ``stream_id`` stream from ``effective_date``."""
        affected = 0
        for flow in self._flows:
            if flow.stream_id != params.stream_id:
                continue
            if flow.date < params.effective_date:
                continue
            if flow.status == FlowStatus.cancelled:
                continue
            if params.adjustment_mode == AdjustmentMode.terminate_stream:
                flow.status = FlowStatus.cancelled
            elif params.adjustment_mode == AdjustmentMode.set_fixed_amount:
                flow.amount = float(params.value)
            else:
                flow.amount = flow.amount * float(params.value)
            affected += 1
        return affected

    # ------------------------------------------------------------------
    # Tool 2: apply_one_off_adjustment
    # ------------------------------------------------------------------
    def apply_one_off_adjustment(self, params: ApplyOneOffAdjustmentParams) -> int:
        """Override one instance, or add a standalone credit/debit."""
        if params.adjustment_type == OneOffAdjustmentType.override_instance_amount:
            candidates = [
                flow
                for flow in self._flows
                if flow.date == params.target_date
                and flow.category == params.target_category
                and flow.status not in {FlowStatus.cancelled, FlowStatus.internal}
            ]
            if not candidates:
                raise ToolApplicationError(
                    f"no instance of {params.target_category!r} on "
                    f"{params.target_date} to override"
                )
            candidates[0].amount = float(params.amount)
            return 1
        direction = (
            FlowDirection.credit
            if params.adjustment_type == OneOffAdjustmentType.add_lump_sum_credit
            else FlowDirection.debit
        )
        self.add_flow(
            direction=direction,
            category=params.target_category,
            amount=params.amount,
            flow_date=params.target_date,
        )
        return 1

    # ------------------------------------------------------------------
    # Tool 3: reschedule_transaction_date
    # ------------------------------------------------------------------
    def reschedule_transaction_date(
        self, params: RescheduleTransactionDateParams
    ) -> int:
        """Move the targeted occurrence(s) to ``new_settlement_date``."""
        if params.related_event_id:
            matches = [
                flow
                for flow in self._flows
                if flow.event_id == params.related_event_id
                or flow.flow_id == params.related_event_id
            ]
        else:
            matches = [
                flow
                for flow in self._flows
                if flow.category == params.category and flow.date == params.original_date
            ]
        if not matches:
            raise ToolApplicationError(
                "no transaction matched the reschedule request"
            )
        for flow in matches:
            flow.date = params.new_settlement_date
        return len(matches)

    # ------------------------------------------------------------------
    # Tool 4: schedule_receivable_or_payable
    # ------------------------------------------------------------------
    def schedule_receivable_or_payable(
        self, params: ScheduleReceivableOrPayableParams
    ) -> int:
        """Insert a new confirmed (optionally recurring) cash flow."""
        interval = params.recurrence_interval_days if params.is_recurring else None
        stream_id = self._new_id("stream") if params.is_recurring else None
        dates = [params.due_or_settlement_date]
        if interval is not None:
            cursor = params.due_or_settlement_date + timedelta(days=interval)
            while cursor <= self.end:
                dates.append(cursor)
                cursor += timedelta(days=interval)
        for occurrence in dates:
            self.add_flow(
                direction=params.direction,
                category=params.category,
                amount=params.amount,
                flow_date=occurrence,
                currency=params.currency,
                interval_days=interval,
                stream_id=stream_id,
            )
        return len(dates)

    # ------------------------------------------------------------------
    # Tool 5: resolve_pending_transaction
    # ------------------------------------------------------------------
    def resolve_pending_transaction(
        self, params: ResolvePendingTransactionParams
    ) -> int:
        """Apply a state transition to a pending/disputed transaction."""
        matches = [
            flow
            for flow in self._flows
            if flow.event_id == params.related_event_id
            or flow.flow_id == params.related_event_id
        ]
        if not matches:
            raise ToolApplicationError(
                f"no pending transaction {params.related_event_id!r}"
            )
        for flow in matches:
            if params.action == PendingAction.confirm_settled:
                if params.settled_amount is not None:
                    flow.amount = float(params.settled_amount)
                flow.status = FlowStatus.settled
            elif params.action == PendingAction.update_final_amount:
                if params.settled_amount is None:
                    raise ToolApplicationError(
                        "settled_amount is required to update a final amount"
                    )
                flow.amount = float(params.settled_amount)
            elif params.action == PendingAction.hold_under_dispute:
                flow.status = FlowStatus.disputed
            else:
                flow.status = FlowStatus.cancelled
        return len(matches)

    # ------------------------------------------------------------------
    # Tool 6: flag_internal_transfer
    # ------------------------------------------------------------------
    def flag_internal_transfer(self, params: FlagInternalTransferParams) -> int:
        """Neutralise the matching internal-transfer legs."""
        matches = [
            flow
            for flow in self._flows
            if flow.internal_ref == params.transaction_ref
            or flow.event_id == params.transaction_ref
        ]
        if params.neutralize_cash_flow:
            for flow in matches:
                flow.status = FlowStatus.internal
        return len(matches)

    # ------------------------------------------------------------------
    # Tool 7: apply_liquidity_hold
    # ------------------------------------------------------------------
    def apply_liquidity_hold(self, params: ApplyLiquidityHoldParams) -> int:
        """Restrict spendable balance until ``release_date``."""
        self._holds.append(
            LiquidityHold(
                hold_id=self._new_id("hold"),
                user_id=self.user_id,
                amount=float(params.hold_amount),
                start_date=self.start,
                release_date=params.release_date,
                reason=params.reason,
            )
        )
        return 1

    # ------------------------------------------------------------------
    # Tool 8: mark_informational_or_unconfirmed
    # ------------------------------------------------------------------
    def mark_informational_or_unconfirmed(
        self, params: MarkInformationalOrUnconfirmedParams
    ) -> int:
        """Log a non-cash or unconfirmed notice; it never touches cash flows."""
        self._notices.append(
            InformationalNotice(
                notice_id=self._new_id("notice"),
                user_id=self.user_id,
                event_type=params.event_type,
                exclude_from_cash_forecast=params.exclude_from_cash_forecast,
                notes=params.notes,
                logged_on=self.start,
            )
        )
        return 1

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def apply(
        self, tool: ToolName | str, params: Mapping[str, Any] | Any
    ) -> int:
        """Validate ``params`` against ``tool`` and apply it to the ledger."""
        name = ToolName(tool)
        handler: Callable[[ForecastLedger, Any], int] = _HANDLERS[name]
        if not isinstance(params, TOOL_PARAMS[name]):
            params = TOOL_PARAMS[name].model_validate(params)
        return handler(self, params)

    def apply_many(
        self, calls: Iterable[tuple[ToolName | str, Mapping[str, Any] | Any]]
    ) -> list[int]:
        """Apply an ordered sequence of tool calls."""
        return [self.apply(tool, params) for tool, params in calls]

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------
    @staticmethod
    def _is_cash_active(flow: ScheduledFlow) -> bool:
        if flow.status in {
            FlowStatus.cancelled,
            FlowStatus.internal,
            FlowStatus.disputed,
        }:
            return False
        if flow.status == FlowStatus.pending and flow.direction == FlowDirection.credit:
            return False
        return True

    def _active_hold_amount(self, day: date) -> float:
        return sum(
            hold.amount
            for hold in self._holds
            if hold.start_date <= day
            and (hold.release_date is None or day <= hold.release_date)
        )

    def forecast(self) -> list[ForecastRow]:
        """Recompute the enriched daily forecast from the current ledger."""
        schedule: dict[date, list[ScheduledFlow]] = {}
        for flow in self._flows:
            if not self._is_cash_active(flow):
                continue
            if self.start <= flow.date <= self.end:
                schedule.setdefault(flow.date, []).append(flow)

        rows: list[ForecastRow] = []
        balance = self.opening_balance
        for day_index in range(self.horizon + 1):
            current = self.start + timedelta(days=day_index)
            todays = schedule.get(current, [])
            inflow = sum(
                flow.amount
                for flow in todays
                if flow.direction == FlowDirection.credit
            )
            outflow = sum(
                flow.amount
                for flow in todays
                if flow.direction == FlowDirection.debit
            )
            balance += inflow - outflow
            hold = self._active_hold_amount(current)
            rows.append(
                ForecastRow(
                    request_id=self.request_id,
                    user_id=self.user_id,
                    currency=self.currency,
                    forecast_date=current,
                    day_index=day_index,
                    inflow=inflow,
                    outflow=outflow,
                    net_flow=inflow - outflow,
                    closing_balance=balance,
                    hold_amount=hold,
                    available_balance=balance - hold,
                )
            )
        return rows

    def to_frame(self) -> pl.DataFrame:
        """Return the enriched forecast as a typed Polars dataframe."""
        return forecast_frame(self.forecast())


_HANDLERS: dict[ToolName, Callable[[ForecastLedger, Any], int]] = {
    ToolName.update_recurring_stream: ForecastLedger.update_recurring_stream,
    ToolName.apply_one_off_adjustment: ForecastLedger.apply_one_off_adjustment,
    ToolName.reschedule_transaction_date: ForecastLedger.reschedule_transaction_date,
    ToolName.schedule_receivable_or_payable: ForecastLedger.schedule_receivable_or_payable,
    ToolName.resolve_pending_transaction: ForecastLedger.resolve_pending_transaction,
    ToolName.flag_internal_transfer: ForecastLedger.flag_internal_transfer,
    ToolName.apply_liquidity_hold: ForecastLedger.apply_liquidity_hold,
    ToolName.mark_informational_or_unconfirmed: (
        ForecastLedger.mark_informational_or_unconfirmed
    ),
}


__all__ = ["DEFAULT_HORIZON_DAYS", "ForecastLedger", "ToolApplicationError"]
