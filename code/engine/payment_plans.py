"""Deterministic generation, safety-checking, and ranking of payment plans.

For a request this module reads the user's financial profile, the payment
options the request allows, and the enriched 90-day forecast ledger, then emits
one safety-checked :class:`PaymentPlanOption` per viable plan:

* ``full_payment`` now (with or without permitted spending changes),
* ``partial_payment`` (a first payment today and the remainder when the full
  amount first becomes safe),
* ``installments`` (exactly matching a supplied payment option), and
* ``wait`` (the full amount paid on the first safe date).

It then applies the problem statement's priority ranking to prune plans that are
strictly worse, leaving only plans on equal footing for the request analyser LLM.
The engine is decoupled from every agent and never decides for the user.
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum
from typing import Any, Mapping

import polars as pl

from ..ingest.models import (
    AffordabilityStatus,
    Direction,
    PaymentMethod,
)
from ..ingest.store import InMemoryDataStore
from ..tools.forecasting import ForecastLedger, FlowDirection
from .plan_models import PaymentPlanOption, plan_options_frame

EPS = 1e-6
MAX_SPENDING_CHANGES = 3
_PAYMENT_TOKENS = "|"

# Internal safety-buffer bar: a no-change plan is treated as fragile when its
# worst balance stays within this fraction of the floor. Kept out of the LLM
# prompt and the output row; used only to decide which plans to offer.
FRAGILITY_CUSHION_RATIO = 0.15


def _tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return {token.strip() for token in str(value).split(_PAYMENT_TOKENS) if token.strip()}


def _fmt(amount: float) -> str:
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _flex_value(value: Any) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else str(value)


def _series(ledger: ForecastLedger) -> list[float]:
    return [row.available_balance for row in ledger.forecast()]


def _evaluate(
    series: list[float],
    payments: list[tuple[int, float]],
    min_balance: float,
    savings: list[float] | None = None,
) -> tuple[float, float]:
    """Return ``(safety_margin, min_available)`` for a plan.

    ``payments`` are ``(day_index, amount)`` debits; a payment on day ``d``
    lowers every day from ``d`` onward. ``savings`` optionally adds a cumulative
    per-day inflow from spending changes.
    """
    days = len(series)
    delta = [0.0] * days
    for index, amount in payments:
        if 0 <= index < days:
            delta[index] += amount
    running = 0.0
    margin = float("inf")
    min_available = float("inf")
    for day in range(days):
        running += delta[day]
        available = series[day] - running + (savings[day] if savings else 0.0)
        min_available = min(min_available, available)
        margin = min(margin, available - min_balance)
    return margin, min_available


def _cumulative_savings(chosen: list[dict[str, Any]], days: int) -> list[float]:
    delta = [0.0] * days
    for action in chosen:
        for day, amount in action["occurrences"]:
            if day < 1 or day >= days:
                continue
            saved = (
                amount
                if action["action"] == "stop"
                else amount - float(action["new_amount"])
            )
            if saved > 0:
                delta[day] += saved
    running = 0.0
    cumulative: list[float] = []
    for day in range(days):
        running += delta[day]
        cumulative.append(running)
    return cumulative


def _max_single_payment(series: list[float], min_balance: float) -> float:
    return max(0.0, min(series) - min_balance)


def _earliest_full_index(
    series: list[float], min_balance: float, requested: float
) -> int | None:
    need = min_balance + requested
    earliest: int | None = None
    running: float | None = None
    for index in range(len(series) - 1, -1, -1):
        running = series[index] if running is None else min(running, series[index])
        if running + EPS >= need:
            earliest = index
    return earliest


def _payment_plan_string(request_date: date, payments: list[tuple[int, float]]) -> str:
    if not payments:
        return "none"
    ordered = sorted(payments, key=lambda item: item[0])
    return "|".join(
        f"{request_date + timedelta(days=index)}:{_fmt(amount)}"
        for index, amount in ordered
    )


def _installment_payments(
    option: Mapping[str, Any], request_date: date, horizon: int
) -> tuple[list[tuple[int, float]], date] | None:
    count = option.get("number_of_payments")
    frequency = option.get("payment_frequency_days")
    first = option.get("first_payment_date")
    amount = option.get("payment_amount")
    if not count or not frequency or first is None or amount is None:
        return None
    payments: list[tuple[int, float]] = []
    last = first
    for step in range(int(count)):
        pay_date = first + timedelta(days=int(frequency) * step)
        index = (pay_date - request_date).days
        if index < 0 or index > horizon:
            return None
        payments.append((index, float(amount)))
        last = pay_date
    return payments, last


class PaymentPlanEngine:
    """Build the candidate payment plans for a request from its forecast ledger."""

    def __init__(self, store: InMemoryDataStore | None = None) -> None:
        self._store = store

    def build(
        self,
        request: Mapping[str, Any],
        profile: Mapping[str, Any],
        ledger: ForecastLedger,
        options: pl.DataFrame | None = None,
    ) -> list[PaymentPlanOption]:
        """Return the pruned list of candidate plans for one request."""
        return build_payment_plan_options(request, profile, ledger, options)

    def build_frame(
        self, results: list[list[PaymentPlanOption]]
    ) -> pl.DataFrame:
        """Flatten per-request option lists into ``payment_plan_options``."""
        return plan_options_frame([option for group in results for option in group])


def build_payment_plan_options(
    request: Mapping[str, Any],
    profile: Mapping[str, Any],
    ledger: ForecastLedger,
    options: pl.DataFrame | None = None,
) -> list[PaymentPlanOption]:
    """Generate, safety-check, rank, and prune a request's payment plans."""
    request_id = str(request["request_id"])
    user_id = str(request["user_id"])
    request_date = request["request_date"]
    requested = float(request["requested_amount"])
    desired = request["desired_completion_date"]
    allows_partial = bool(request["allows_partial_payment"])
    min_balance = float(profile["minimum_balance_to_keep"])
    considered = _tokens(profile["payment_methods_user_will_consider"])
    max_installment_months = profile["max_installment_months"]

    series = _series(ledger)
    horizon = ledger.horizon
    deadline_index = min((desired - request_date).days, horizon)

    amount_safe = min(_max_single_payment(series, min_balance), requested)
    earliest_index = _earliest_full_index(series, min_balance, requested)
    earliest_date = (
        request_date + timedelta(days=earliest_index)
        if earliest_index is not None
        else None
    )
    full_safe_now = amount_safe + EPS >= requested

    plans: list[PaymentPlanOption] = []

    def add(
        method: PaymentMethod,
        status: AffordabilityStatus,
        payments: list[tuple[int, float]] | None,
        *,
        spending_changes: str = "none",
        option_id: str | None = None,
        savings: list[float] | None = None,
        rationale: str,
    ) -> None:
        payments = payments or []
        margin, min_available = _evaluate(
            series, payments, min_balance, savings=savings
        )
        if margin < -EPS:
            return
        plan_string = _payment_plan_string(request_date, payments)
        first_index = min((index for index, _ in payments), default=None)
        plans.append(
            PaymentPlanOption(
                request_id=request_id,
                user_id=user_id,
                plan_id="pending",
                payment_method=method,
                affordability_status=status,
                amount_safe_to_pay=amount_safe,
                payment_plan=plan_string,
                earliest_date_for_full_payment=(
                    request_date if earliest_index == 0 else earliest_date
                ),
                spending_changes_needed=spending_changes,
                total_paid=round(sum(amount for _, amount in payments), 2),
                first_payment_date=(
                    request_date + timedelta(days=first_index)
                    if first_index is not None
                    else None
                ),
                payment_count=len(payments),
                payment_option_id=option_id,
                completes_by_deadline=True,
                uses_spending_changes=spending_changes != "none",
                min_available_after_plan=round(min_available, 2),
                rationale=rationale,
            )
        )

    # 1. Full payment today, with no spending changes.
    if "full_payment" in considered and full_safe_now:
        add(
            PaymentMethod.full_payment,
            AffordabilityStatus.affordable_now,
            [(0, requested)],
            rationale="Full amount is safe on the request date with no spending changes.",
        )

    # 2. Wait: full amount paid on the first safe date, on or before the deadline.
    if (
        "full_payment" in considered
        and earliest_index is not None
        and earliest_index > 0
        and earliest_index <= deadline_index
    ):
        add(
            PaymentMethod.wait,
            AffordabilityStatus.affordable_later,
            [(earliest_index, requested)],
            rationale=(
                f"Full amount first becomes safe on {earliest_date}, on or "
                "before the desired completion date."
            ),
        )

    # 3. Partial payment: part today, remainder on the first safe full date.
    if (
        allows_partial
        and "partial_payment" in considered
        and 0 < amount_safe < requested - EPS
        and earliest_index is not None
        and earliest_index <= deadline_index
    ):
        remainder = requested - amount_safe
        add(
            PaymentMethod.partial_payment,
            AffordabilityStatus.affordable_with_plan,
            [(0, amount_safe), (earliest_index, remainder)],
            rationale=(
                f"Pay {_fmt(amount_safe)} today and the remaining "
                f"{_fmt(remainder)} on {earliest_date}, on or before the deadline."
            ),
        )

    # 4. Installments: exactly match a supplied payment option.
    if "installments" in considered and max_installment_months:
        option_rows = (
            options.filter(
                (pl.col("request_id") == request_id)
                & (pl.col("payment_method") == PaymentMethod.installments.value)
            ).iter_rows(named=True)
            if options is not None
            else []
        )
        for option in option_rows:
            count = option.get("number_of_payments")
            if not count or int(count) > int(max_installment_months):
                continue
            schedule = _installment_payments(option, request_date, horizon)
            if schedule is None:
                continue
            payments, last_date = schedule
            if last_date > desired:
                continue
            add(
                PaymentMethod.installments,
                AffordabilityStatus.affordable_with_plan,
                payments,
                option_id=str(option["payment_option_id"]),
                rationale=(
                    f"{int(count)} installments matching option "
                    f"{option['payment_option_id']}, completing on {last_date}."
                ),
            )

    # 5. Full payment today funded by permitted spending changes. This is
    #    required when no no-change plan can complete the request, and it is
    #    also offered as a conservative alternative when one can, so the
    #    request analyser can trade a small amount of spending for a larger
    #    safety buffer when the user is under financial stress.
    if not plans:
        change_plan = _spending_change_full(
            request_id,
            user_id,
            requested,
            request_date,
            series,
            min_balance,
            profile,
            ledger,
            earliest_index,
            earliest_date,
            amount_safe,
        )
        if change_plan is not None:
            plans.append(change_plan)

    conservative = _conservative_change_plan(
        request_id,
        user_id,
        requested,
        request_date,
        series,
        min_balance,
        profile,
        ledger,
        earliest_index,
        earliest_date,
        amount_safe,
    )
    if conservative is not None and not any(
        plan.spending_changes_needed == conservative.spending_changes_needed
        and plan.payment_plan == conservative.payment_plan
        for plan in plans
    ):
        plans.append(conservative)

    # 6. Fallback: nothing safe and eligible completes the request.
    if not plans:
        plans.append(
            PaymentPlanOption(
                request_id=request_id,
                user_id=user_id,
                plan_id="pending",
                payment_method=PaymentMethod.not_recommended,
                affordability_status=AffordabilityStatus.not_affordable,
                amount_safe_to_pay=amount_safe,
                payment_plan="none",
                earliest_date_for_full_payment=(
                    request_date if earliest_index == 0 else earliest_date
                ),
                spending_changes_needed="none",
                total_paid=0.0,
                first_payment_date=None,
                payment_count=0,
                payment_option_id=None,
                completes_by_deadline=False,
                uses_spending_changes=False,
                min_available_after_plan=round(min(series), 2),
                rationale=(
                    "No eligible plan completes the request by the desired "
                    "completion date while keeping the minimum balance."
                ),
            )
        )

    return _prune(plans)


def _rank_key(option: PaymentPlanOption) -> tuple[Any, ...]:
    return (
        option.uses_spending_changes,
        round(option.total_paid, 2),
        option.first_payment_date or date.max,
        option.payment_count,
        option.payment_option_id or "",
    )


def _prune(plans: list[PaymentPlanOption]) -> list[PaymentPlanOption]:
    """Keep the best plan of each family (no-change and spending-change).

    Retaining one representative per family lets the request analyser choose a
    conservative, spending-change plan under financial stress while the ranking
    still removes every strictly worse duplicate.
    """
    completing = [plan for plan in plans if plan.completes_by_deadline]
    pool = completing or plans
    kept: list[PaymentPlanOption] = []
    for family in (False, True):
        family_plans = [plan for plan in pool if plan.uses_spending_changes is family]
        if not family_plans:
            continue
        family_plans.sort(key=_rank_key)
        best = _rank_key(family_plans[0])
        kept.extend(
            plan for plan in family_plans if _rank_key(plan)[:4] == best[:4]
        )
    if not kept:
        kept = sorted(pool, key=_rank_key)[:1]
    for index, plan in enumerate(kept, start=1):
        plan.plan_id = f"plan_{index:02d}"
    return kept


def _change_candidates(
    request_date: date, profile: Mapping[str, Any], ledger: ForecastLedger
) -> list[dict[str, Any]]:
    """Return the permitted, flexible debit streams that may be changed."""
    protected = _tokens(profile["expense_categories_to_protect"])
    stop_categories = _tokens(
        profile["expense_categories_user_is_willing_to_stop"]
    )
    reduce_categories = _tokens(
        profile["expense_categories_user_is_willing_to_reduce"]
    )

    streams: dict[str, list[Any]] = {}
    for flow in ledger.flows:
        if flow.stream_id is None or flow.direction != FlowDirection.debit:
            continue
        streams.setdefault(flow.stream_id, []).append(flow)

    candidates: list[dict[str, Any]] = []
    for stream_id, flows in streams.items():
        category = flows[0].category
        if category in protected:
            continue
        flexibility = _flex_value(flows[0].flexibility)
        event_id = flows[0].event_id
        if event_id is None:
            continue
        can_stop = category in stop_categories and flexibility in {
            "stoppable",
            "reducible_or_stoppable",
        }
        can_reduce = category in reduce_categories and flexibility in {
            "reducible",
            "reducible_or_stoppable",
        }
        if not (can_stop or can_reduce):
            continue
        occurrences = [
            ((flow.date - request_date).days, float(flow.amount)) for flow in flows
        ]
        candidates.append(
            {
                "stream_id": stream_id,
                "category": category,
                "description": flows[0].description,
                "event_id": str(event_id),
                "amount": float(flows[0].amount),
                "flexibility": flexibility,
                "occurrences": occurrences,
                "can_stop": can_stop,
                "can_reduce": can_reduce,
                "action": None,
                "new_amount": None,
            }
        )
    return candidates


def _spending_changes_string(chosen: list[dict[str, Any]]) -> str:
    return "|".join(
        f"stop:{action['event_id']}"
        if action["action"] == "stop"
        else f"reduce_to:{action['event_id']}:{_fmt(action['new_amount'])}"
        for action in chosen
    )


def _spending_details_string(chosen: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for action in chosen:
        verb = (
            "stop"
            if action["action"] == "stop"
            else f"reduce to {_fmt(action['new_amount'])}"
        )
        label = action.get("description") or action.get("category") or ""
        parts.append(f"{verb} {action['category']} ({label})")
    return "; ".join(parts)


def _conservative_change_plan(
    request_id: str,
    user_id: str,
    requested: float,
    request_date: date,
    series: list[float],
    min_balance: float,
    profile: Mapping[str, Any],
    ledger: ForecastLedger,
    earliest_index: int | None,
    earliest_date: date | None,
    amount_safe: float,
) -> PaymentPlanOption | None:
    """Offer a conservative full payment that also curbs permitted spending.

    This is not required for safety in the optimists' forecast, but it lets the
    request analyser choose a larger buffer when the user is under financial
    stress (a pay cut or emergency). The changes are limited to categories the
    user permits and to flexible streams, and the plan stays safe.
    """
    days = len(series)
    candidates = _change_candidates(request_date, profile, ledger)
    if not candidates:
        return None

    chosen: list[dict[str, Any]] = []
    used: set[str] = set()
    target_cushion = FRAGILITY_CUSHION_RATIO * min_balance
    for _ in range(MAX_SPENDING_CHANGES):
        # Stop as soon as the plan clears the fragility bar: apply the fewest,
        # most targeted changes needed rather than every permitted one.
        savings = _cumulative_savings(chosen, days)
        _, current_min = _evaluate(
            series, [(0, requested)], min_balance, savings
        )
        if current_min - min_balance >= target_cushion - EPS:
            break
        best: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        for candidate in candidates:
            if candidate["stream_id"] in used:
                continue
            total = sum(
                amount
                for day, amount in candidate["occurrences"]
                if 1 <= day < days
            )
            if total <= 0:
                continue
            key = (-total, candidate["event_id"])
            if best is None or key < best[0]:
                best = (key, candidate)
        if best is None:
            break
        candidate = best[1]
        if candidate["can_stop"] and candidate["flexibility"] in {
            "stoppable",
            "reducible_or_stoppable",
        }:
            candidate["action"] = "stop"
        elif candidate["can_reduce"]:
            candidate["action"] = "reduce"
            candidate["new_amount"] = max(0.0, candidate["amount"] * 0.5)
        else:
            candidate["action"] = "stop"
        used.add(candidate["stream_id"])
        chosen.append(candidate)
    if not chosen:
        return None

    savings = _cumulative_savings(chosen, days)
    margin, min_available = _evaluate(series, [(0, requested)], min_balance, savings)
    if margin < -EPS:
        return None

    changes = _spending_changes_string(chosen)
    details = _spending_details_string(chosen)
    return PaymentPlanOption(
        request_id=request_id,
        user_id=user_id,
        plan_id="pending",
        payment_method=PaymentMethod.full_payment,
        affordability_status=AffordabilityStatus.affordable_with_plan,
        amount_safe_to_pay=amount_safe,
        payment_plan=_payment_plan_string(request_date, [(0, requested)]),
        earliest_date_for_full_payment=(
            request_date if earliest_index == 0 else earliest_date
        ),
        spending_changes_needed=changes,
        spending_change_details=details,
        total_paid=round(requested, 2),
        first_payment_date=request_date,
        payment_count=1,
        payment_option_id=None,
        completes_by_deadline=True,
        uses_spending_changes=True,
        min_available_after_plan=round(min_available, 2),
        rationale=(
            f"Full amount is safe on the request date after permitted "
            f"spending changes: {changes}."
        ),
    )


def _spending_change_full(
    request_id: str,
    user_id: str,
    requested: float,
    request_date: date,
    series: list[float],
    min_balance: float,
    profile: Mapping[str, Any],
    ledger: ForecastLedger,
    earliest_index: int | None,
    earliest_date: date | None,
    amount_safe: float,
) -> PaymentPlanOption | None:
    days = len(series)
    candidates = _change_candidates(request_date, profile, ledger)
    if not candidates:
        return None

    chosen: list[dict[str, Any]] = []
    used: set[str] = set()
    for _ in range(MAX_SPENDING_CHANGES):
        savings = _cumulative_savings(chosen, days)
        margins = [series[day] - requested + savings[day] - min_balance for day in range(days)]
        worst = min(margins)
        if worst >= -EPS:
            break
        worst_day = margins.index(worst)
        deficit = -worst

        best: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        for candidate in candidates:
            if candidate["stream_id"] in used:
                continue
            before = [
                amount
                for day, amount in candidate["occurrences"]
                if 1 <= day <= worst_day
            ]
            impact = 0.0
            if candidate["can_stop"]:
                impact = sum(before)
            if candidate["can_reduce"]:
                impact = max(impact, sum(min(amount, deficit) for amount in before))
            if impact <= 0:
                continue
            key = (-impact, candidate["event_id"])
            if best is None or key < best[0]:
                best = (key, candidate)
        if best is None:
            break
        candidate = best[1]
        if candidate["can_reduce"] and candidate["flexibility"] == "reducible_or_stoppable":
            candidate["action"] = "reduce"
        elif candidate["can_stop"]:
            candidate["action"] = "stop"
        else:
            candidate["action"] = "reduce"
        if candidate["action"] == "reduce":
            candidate["new_amount"] = max(0.0, candidate["amount"] - deficit)
        used.add(candidate["stream_id"])
        chosen.append(candidate)

    if not chosen:
        return None
    savings = _cumulative_savings(chosen, days)
    margin, min_available = _evaluate(series, [(0, requested)], min_balance, savings)
    if margin < -EPS:
        return None

    changes = _spending_changes_string(chosen)
    details = _spending_details_string(chosen)
    return PaymentPlanOption(
        request_id=request_id,
        user_id=user_id,
        plan_id="pending",
        payment_method=PaymentMethod.full_payment,
        affordability_status=AffordabilityStatus.affordable_with_plan,
        amount_safe_to_pay=amount_safe,
        payment_plan=_payment_plan_string(request_date, [(0, requested)]),
        earliest_date_for_full_payment=(
            request_date if earliest_index == 0 else earliest_date
        ),
        spending_changes_needed=changes,
        spending_change_details=details,
        total_paid=round(requested, 2),
        first_payment_date=request_date,
        payment_count=1,
        payment_option_id=None,
        completes_by_deadline=True,
        uses_spending_changes=True,
        min_available_after_plan=round(min_available, 2),
        rationale=(
            f"Full amount is safe on the request date after spending changes: {changes}."
        ),
    )


__all__ = [
    "PaymentPlanEngine",
    "FRAGILITY_CUSHION_RATIO",
    "build_payment_plan_options",
]
