"""Async request analyser: choose one candidate plan and write one output row.

The analyser is fully decoupled from the engine. It receives the engine's
``payment_plan_options`` dataframe, joins each request with the user's profile,
and asks the LLM to select one already-validated option and explain it. The
deterministic output fields are copied from the chosen option so the model can
never drift from what the engine verified.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import polars as pl
from pydantic import BaseModel, ValidationError

from ...connectors import OpenAIConnector
from ...ingest.pipeline import DEFAULT_DATASET_DIR, load_dataset
from ...evaluation.usage import AGENT_REQUEST, usage_row
from ...ingest.store import InMemoryDataStore
from ...engine.plan_models import PaymentPlanOption
from ...engine.payment_plans import FRAGILITY_CUSHION_RATIO
from ...tools import ConversionRateError, CurrencyConverter
from ..cash_forecaster.config import SALARY_TERMINATION_DESCRIPTION
from .models import (
    RequestAnalyserResult,
    RequestOutputRow,
    output_frame,
    usage_frame,
)
from .schema import get_input_model, get_output_model, load_system_prompt

USER_PROMPT = (
    "Decide the single most responsible payment plan for this request and return "
    "the strict JSON output row. Decide for yourself; the options are not ranked "
    "for you. Weigh how close each plan brings the balance to the floor, the "
    "amount the user normally spends on protected categories, and the user's "
    "willingness to reduce or stop other spending. Copy the deterministic fields "
    "verbatim from the chosen option and write a brief decision_explanation."
)
DEFAULT_CONCURRENCY = 4

_ERROR_DTYPES: dict[str, Any] = {
    "request_id": pl.String,
    "error": pl.String,
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class RequestAnalyser:
    """Async agent that turns candidate plans into final output rows."""

    def __init__(
        self,
        plan_options: pl.DataFrame,
        store: InMemoryDataStore | None = None,
        connector: OpenAIConnector | None = None,
        dataset_dir: Path | str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._dataset_dir = (
            Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
        )
        self._store = store or load_dataset(self._dataset_dir)
        self._plan_options = plan_options
        self._connector = connector
        self._concurrency = max(1, concurrency)
        self._system_prompt = load_system_prompt()
        self._input_model = get_input_model()
        self._output_model = get_output_model()

    @property
    def store(self) -> InMemoryDataStore:
        return self._store

    def _connector_or_default(self) -> OpenAIConnector:
        if self._connector is None:
            self._connector = OpenAIConnector()
        return self._connector

    def _profile(self, user_id: str) -> Mapping[str, Any]:
        matches = self._store.table("financial_profiles").filter(
            pl.col("user_id") == user_id
        )
        if matches.height == 0:
            raise KeyError(f"no financial profile for {user_id!r}")
        return matches.row(0, named=True)

    def _options_for(self, request_id: str) -> list[PaymentPlanOption]:
        rows = self._plan_options.filter(pl.col("request_id") == request_id)
        return [
            PaymentPlanOption.model_validate(row)
            for row in rows.iter_rows(named=True)
        ]

    def _is_income_reduction(self, user_id: str, params_json: str) -> bool:
        """True when a credit-stream update lowers the user's income."""
        try:
            params = json.loads(params_json or "{}")
        except (TypeError, ValueError):
            return True
        mode = params.get("adjustment_mode")
        if mode == "terminate_stream":
            return True
        value = params.get("value")
        if mode == "percentage_multiplier":
            return value is not None and float(value) < 1.0
        if mode == "set_fixed_amount" and value is not None:
            events = self._store.table("financial_events").filter(
                (pl.col("user_id") == user_id)
                & (pl.col("category") == "salary")
                & (pl.col("direction") == "credit")
                & pl.col("amount").is_not_null()
            )
            if events.height:
                historical_max = float(events["amount"].max())
                return float(value) < historical_max * 0.995
        return False

    def _financial_context(
        self, user_id: str, request: Mapping[str, Any]
    ) -> tuple[bool, list[dict[str, str]]]:
        """Return a stress flag and evidence drawn from applied message calls.

        A pay cut, terminated income, liquidity hold, disputed/cancelled
        settlement, or an emergency request all mean the user should be advised
        conservatively. The evidence is a short, grounded summary the analyser
        uses to justify a safer plan.
        """
        evidence: list[dict[str, str]] = []
        request_type = str(request.get("request_type"))
        stress = request_type == "emergency_expense"
        if stress:
            evidence.append(
                {
                    "source": "request",
                    "summary": "Request type is emergency_expense.",
                }
            )
        # A settled terminal paycheck means the user's salary has ended, which is
        # a first-class stress signal even with no message attached.
        terminal = self._store.table("financial_events").filter(
            (pl.col("user_id") == user_id)
            & (pl.col("category") == "salary")
            & (pl.col("description") == SALARY_TERMINATION_DESCRIPTION)
        )
        if terminal.height:
            stress = True
            terminated_on = max(terminal["event_date"].to_list())
            evidence.append(
                {
                    "source": "financial_events",
                    "summary": (
                        "Salary terminated (Final employer payroll) on "
                        f"{terminated_on}."
                    ),
                }
            )
        # A settled salary that has stepped down from its earlier level is a pay
        # cut, regardless of whether a message or tool call recorded it.
        salary = self._store.table("financial_events").filter(
            (pl.col("user_id") == user_id)
            & (pl.col("category") == "salary")
            & (pl.col("direction") == "credit")
            & (pl.col("status") == "settled")
            & pl.col("amount").is_not_null()
        ).sort("event_date")
        if salary.height >= 3:
            amounts = [float(value) for value in salary["amount"].to_list()]
            historical_max = max(amounts)
            recent_peak = max(amounts[-2:])
            if recent_peak < historical_max * 0.995:
                stress = True
                evidence.append(
                    {
                        "source": "financial_events",
                        "summary": (
                            f"Salary stepped down from {historical_max:.2f} to "
                            f"{recent_peak:.2f}."
                        ),
                    }
                )
        if "message_analysis" not in self._store.keys():
            return stress, evidence
        frame = self._store.table("message_analysis")
        if frame.height == 0:
            return stress, evidence
        rows = frame.filter(
            (pl.col("user_id") == user_id) & (pl.col("applied") == True)  # noqa: E712
        )
        for row in rows.iter_rows(named=True):
            tool = row.get("tool")
            if not tool:
                continue
            params = row.get("params_json") or ""
            evidence.append(
                {"source": "message", "summary": f"Applied {tool}: {params}"}
            )
            if tool == "update_recurring_stream" and (
                "salary" in params or "credit" in params
            ):
                if self._is_income_reduction(user_id, params):
                    stress = True
            elif tool == "apply_liquidity_hold":
                stress = True
            elif tool == "resolve_pending_transaction" and (
                "hold_under_dispute" in params or "cancel_event" in params
            ):
                stress = True
        return stress, evidence

    def _protected_spend(
        self, profile: Mapping[str, Any], request: Mapping[str, Any]
    ) -> dict[str, float]:
        """Return the user's average monthly spend per protected category.

        This is the essential run-rate the analyser should not erode, computed
        from settled debits over the trailing 90 days in the home currency.
        """
        protected = {
            token.strip()
            for token in (profile["expense_categories_to_protect"] or "").split("|")
            if token.strip()
        }
        if not protected:
            return {}
        home = profile["home_currency"]
        request_date = request["request_date"]
        since = request_date - timedelta(days=90)
        events = self._store.table("financial_events").filter(
            (pl.col("user_id") == request["user_id"])
            & (pl.col("direction") == "debit")
            & (pl.col("status") == "settled")
            & (pl.col("event_date") <= request_date)
            & (pl.col("event_date") > since)
            & pl.col("category").is_in(sorted(protected))
            & pl.col("amount").is_not_null()
        )
        converter = CurrencyConverter.from_store(self._store)
        spend: dict[str, float] = {}
        for row in events.iter_rows(named=True):
            amount = float(row["amount"])
            if row["currency"] != home:
                try:
                    amount = converter.convert(
                        amount,
                        row["currency"],
                        home,
                        row["settlement_date"] or row["event_date"],
                    )
                except ConversionRateError:
                    continue
            spend[row["category"]] = spend.get(row["category"], 0.0) + amount
        return {
            category: round(total / 3.0, 2)
            for category, total in sorted(spend.items())
        }

    def _build_prompt(
        self,
        request: Mapping[str, Any],
        profile: Mapping[str, Any],
        options: Sequence[PaymentPlanOption],
        stress: bool,
        evidence: Sequence[Mapping[str, str]],
        protected_spend: Mapping[str, float],
    ) -> str:
        minimum = float(profile["minimum_balance_to_keep"])
        protected_total = sum(protected_spend.values())
        payload = {
            "request_id": request["request_id"],
            "user_id": request["user_id"],
            "request_date": _jsonable(request["request_date"]),
            "request_type": request["request_type"],
            "requested_amount": request["requested_amount"],
            "desired_completion_date": _jsonable(
                request["desired_completion_date"]
            ),
            "allows_partial_payment": request["allows_partial_payment"],
            "request_text": request["request_text"],
            "home_currency": profile["home_currency"],
            "current_available_balance": profile["current_available_balance"],
            "minimum_balance_to_keep": minimum,
            "financial_priorities": profile["financial_priorities"],
            "expense_categories_to_protect": profile[
                "expense_categories_to_protect"
            ],
            "protected_category_monthly_spend": dict(protected_spend),
            "expense_categories_user_is_willing_to_reduce": profile[
                "expense_categories_user_is_willing_to_reduce"
            ],
            "expense_categories_user_is_willing_to_stop": profile[
                "expense_categories_user_is_willing_to_stop"
            ],
            "payment_methods_user_will_consider": profile[
                "payment_methods_user_will_consider"
            ],
            "max_installment_months": profile["max_installment_months"],
            "financial_stress": stress,
            "financial_evidence": list(evidence),
            "protected_monthly_total": round(protected_total, 2),
            "payment_plan_options": [
                {key: _jsonable(value) for key, value in option.to_row().items()}
                for option in options
            ],
        }
        return (
            f"{USER_PROMPT}\n\n"
            "INPUT (request, profile, protected spend, financial evidence, and "
            "pre-validated payment_plan_options):\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        )

    def _choose(
        self,
        decision: BaseModel,
        options: Sequence[PaymentPlanOption],
    ) -> PaymentPlanOption:
        selected = getattr(decision, "selected_plan_id", None)
        for option in options:
            if option.plan_id == selected:
                return option
        method = getattr(decision, "recommended_payment_method", None)
        if method is not None:
            value = getattr(method, "value", method)
            plan = str(getattr(decision, "payment_plan", ""))
            for option in options:
                if (
                    option.payment_method.value == str(value)
                    and option.payment_plan == plan
                ):
                    return option
        return options[0]

    @staticmethod
    def _gate_options(
        options: Sequence[PaymentPlanOption],
        minimum: float,
        protected_total: float,
        stress: bool,
    ) -> list[PaymentPlanOption]:
        """Offer spending-change plans only when the no-change plan is fragile.

        A precautionary spending change is only a responsible option when the
        no-change plan leaves a thin cushion. Withholding it otherwise keeps the
        model from eroding the user's discretionary spending for no benefit,
        while it still decides freely between the plans that remain.
        """
        no_change = [o for o in options if not o.uses_spending_changes]
        if not no_change:
            return list(options)
        cushion = max(o.min_available_after_plan for o in no_change) - minimum
        fragile = cushion < FRAGILITY_CUSHION_RATIO * minimum
        if stress and protected_total > 0 and cushion < protected_total:
            fragile = True
        return list(options) if fragile else no_change

    def _compose(
        self,
        chosen: PaymentPlanOption,
        explanation: str | None,
    ) -> RequestOutputRow:
        explanation = (explanation or "").strip() or chosen.rationale
        return RequestOutputRow(
            request_id=chosen.request_id,
            amount_safe_to_pay=chosen.amount_safe_to_pay,
            affordability_status=chosen.affordability_status,
            recommended_payment_method=chosen.payment_method,
            payment_plan=chosen.payment_plan,
            earliest_date_for_full_payment=chosen.earliest_date_for_full_payment,
            spending_changes_needed=chosen.spending_changes_needed,
            decision_explanation=explanation,
        )

    async def _analyse_one(
        self, request: Mapping[str, Any]
    ) -> tuple[RequestOutputRow | None, dict[str, Any] | None, str | None]:
        request_id = str(request["request_id"])
        options = self._options_for(request_id)
        if not options:
            return None, None, "no payment_plan_options for request"

        profile = self._profile(str(request["user_id"]))
        stress, evidence = self._financial_context(
            str(request["user_id"]), request
        )
        protected_spend = self._protected_spend(profile, request)
        options = self._gate_options(
            options,
            float(profile["minimum_balance_to_keep"]),
            sum(protected_spend.values()),
            stress,
        )
        prompt = self._build_prompt(
            request, profile, options, stress, evidence, protected_spend
        )
        try:
            result = await self._connector_or_default().generate_structured_detailed(
                prompt, self._output_model, system=self._system_prompt
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the best option
            chosen = options[0]
            row = self._compose(chosen, chosen.rationale)
            return row, None, f"{type(exc).__name__}: {exc}"

        decision = result.parsed
        chosen = self._choose(decision, options)
        row = self._compose(
            chosen, getattr(decision, "decision_explanation", None)
        )
        usage = usage_row(AGENT_REQUEST, result.model, result.usage, request_id)
        return row, usage, None

    async def _analyse_many(
        self,
        requests: list[Mapping[str, Any]],
        progress: Callable[[int, int], None] | None = None,
    ) -> list[tuple[RequestOutputRow | None, dict[str, Any] | None, str | None]]:
        semaphore = asyncio.Semaphore(self._concurrency)
        total = len(requests)
        done = 0
        # Rows are stored by their input position as they arrive, so the final
        # frame keeps the dataset's request order regardless of completion order.
        results: list[Any] = [None] * total

        async def guarded(index: int, request: Mapping[str, Any]) -> None:
            nonlocal done
            async with semaphore:
                outcome = await self._analyse_one(request)
            results[index] = outcome
            done += 1
            if progress is not None:
                progress(done, total)

        await asyncio.gather(
            *(guarded(index, request) for index, request in enumerate(requests))
        )
        return results  # type: ignore[return-value]

    async def run(
        self,
        request_ids: Sequence[str] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> RequestAnalyserResult:
        """Analyse the selected requests and return the ``output.csv`` frame."""
        requests = self._store.table("requests")
        if request_ids is not None:
            requests = requests.filter(
                pl.col("request_id").is_in(list(request_ids))
            )
        # Preserve the original dataset (requests.csv) order.
        rows = list(requests.iter_rows(named=True))

        outcomes = await self._analyse_many(rows, progress)
        output_rows: list[RequestOutputRow] = []
        usages: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for request, (row, usage, error) in zip(rows, outcomes):
            if row is not None:
                output_rows.append(row)
            if usage is not None:
                usages.append(usage)
            if error is not None:
                errors.append(
                    {"request_id": str(request["request_id"]), "error": error}
                )
        return RequestAnalyserResult(
            output=output_frame(output_rows),
            usage=usage_frame(usages),
            errors=pl.DataFrame(errors, schema=_ERROR_DTYPES, orient="row"),
            store=self._store,
        )


__all__ = ["DEFAULT_CONCURRENCY", "RequestAnalyser", "USER_PROMPT"]
