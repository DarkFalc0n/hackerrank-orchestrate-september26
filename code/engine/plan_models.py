"""Pydantic models and dataframe schema for candidate payment plans.

The engine emits one :class:`PaymentPlanOption` per candidate plan. These rows
are the ``payment_plan_options`` dataframe handed to the request analyser LLM.
The engine never decides; it only generates, ranks, and prunes safe options.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from ..ingest.models import AffordabilityStatus, PaymentMethod

PLAN_OPTION_DTYPES: dict[str, Any] = {
    "request_id": pl.String,
    "user_id": pl.String,
    "plan_id": pl.String,
    "payment_method": pl.String,
    "affordability_status": pl.String,
    "amount_safe_to_pay": pl.Float64,
    "payment_plan": pl.String,
    "earliest_date_for_full_payment": pl.Date,
    "spending_changes_needed": pl.String,
    "spending_change_details": pl.String,
    "total_paid": pl.Float64,
    "first_payment_date": pl.Date,
    "payment_count": pl.Int64,
    "payment_option_id": pl.String,
    "completes_by_deadline": pl.Boolean,
    "uses_spending_changes": pl.Boolean,
    "min_available_after_plan": pl.Float64,
    "rationale": pl.String,
}


class PaymentPlanOption(BaseModel):
    """One deterministic, safety-checked candidate plan for a request."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    user_id: str
    plan_id: str
    payment_method: PaymentMethod
    affordability_status: AffordabilityStatus
    amount_safe_to_pay: float = Field(ge=0)
    payment_plan: str
    earliest_date_for_full_payment: date | None = None
    spending_changes_needed: str = "none"
    spending_change_details: str = ""
    total_paid: float = Field(ge=0)
    first_payment_date: date | None = None
    payment_count: int = Field(ge=0)
    payment_option_id: str | None = None
    completes_by_deadline: bool
    uses_spending_changes: bool
    min_available_after_plan: float
    rationale: str

    def to_row(self) -> dict[str, Any]:
        """Flatten to a row matching :data:`PLAN_OPTION_DTYPES`."""
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "plan_id": self.plan_id,
            "payment_method": self.payment_method.value,
            "affordability_status": self.affordability_status.value,
            "amount_safe_to_pay": self.amount_safe_to_pay,
            "payment_plan": self.payment_plan,
            "earliest_date_for_full_payment": self.earliest_date_for_full_payment,
            "spending_changes_needed": self.spending_changes_needed,
            "spending_change_details": self.spending_change_details,
            "total_paid": self.total_paid,
            "first_payment_date": self.first_payment_date,
            "payment_count": self.payment_count,
            "payment_option_id": self.payment_option_id,
            "completes_by_deadline": self.completes_by_deadline,
            "uses_spending_changes": self.uses_spending_changes,
            "min_available_after_plan": self.min_available_after_plan,
            "rationale": self.rationale,
        }


def plan_options_frame(options: list[PaymentPlanOption]) -> pl.DataFrame:
    """Build the typed ``payment_plan_options`` dataframe."""
    return pl.DataFrame(
        [option.to_row() for option in options],
        schema=PLAN_OPTION_DTYPES,
        orient="row",
    )


__all__ = [
    "PLAN_OPTION_DTYPES",
    "PaymentPlanOption",
    "plan_options_frame",
]
