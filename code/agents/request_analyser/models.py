"""Pydantic records and dataframe schemas for the request analyser agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from ...evaluation.usage import USAGE_DTYPES, usage_frame
from ...ingest.models import AffordabilityStatus, PaymentMethod

OUTPUT_DTYPES: dict[str, Any] = {
    "request_id": pl.String,
    "amount_safe_to_pay": pl.Float64,
    "affordability_status": pl.String,
    "recommended_payment_method": pl.String,
    "payment_plan": pl.String,
    "earliest_date_for_full_payment": pl.Date,
    "spending_changes_needed": pl.String,
    "decision_explanation": pl.String,
}

class RequestOutputRow(BaseModel):
    """One completed ``output.csv`` row."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    amount_safe_to_pay: float = Field(ge=0)
    affordability_status: AffordabilityStatus
    recommended_payment_method: PaymentMethod
    payment_plan: str
    earliest_date_for_full_payment: date | None = None
    spending_changes_needed: str = "none"
    decision_explanation: str

    def to_row(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "amount_safe_to_pay": self.amount_safe_to_pay,
            "affordability_status": self.affordability_status.value,
            "recommended_payment_method": self.recommended_payment_method.value,
            "payment_plan": self.payment_plan,
            "earliest_date_for_full_payment": self.earliest_date_for_full_payment,
            "spending_changes_needed": self.spending_changes_needed,
            "decision_explanation": self.decision_explanation,
        }


def output_frame(rows: list[RequestOutputRow]) -> pl.DataFrame:
    """Build the typed ``output.csv`` dataframe."""
    return pl.DataFrame(
        [row.to_row() for row in rows],
        schema=OUTPUT_DTYPES,
        orient="row",
    )


@dataclass
class RequestAnalyserResult:
    """Outcome of a request-analyser run."""

    output: pl.DataFrame
    usage: pl.DataFrame
    errors: pl.DataFrame
    store: Any


__all__ = [
    "OUTPUT_DTYPES",
    "USAGE_DTYPES",
    "RequestAnalyserResult",
    "RequestOutputRow",
    "output_frame",
    "usage_frame",
]
