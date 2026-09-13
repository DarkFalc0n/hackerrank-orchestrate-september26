"""Table schema definitions for every participant-facing dataset file."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

DTypeLike = pl.DataType | type[pl.DataType]


@dataclass(frozen=True)
class TableSchema:
    name: str
    file: str
    dtypes: dict[str, DTypeLike] = field(default_factory=dict)

    @property
    def columns(self) -> list[str]:
        return list(self.dtypes)

    def cast_dtypes(self) -> dict[str, DTypeLike]:
        return dict(self.dtypes)


FINANCIAL_PROFILES = TableSchema(
    name="financial_profiles",
    file="financial_profiles.csv",
    dtypes={
        "user_id": pl.String,
        "home_currency": pl.String,
        "current_available_balance": pl.Float64,
        "minimum_balance_to_keep": pl.Float64,
        "financial_priorities": pl.String,
        "expense_categories_to_protect": pl.String,
        "expense_categories_user_is_willing_to_reduce": pl.String,
        "expense_categories_user_is_willing_to_stop": pl.String,
        "payment_methods_user_will_consider": pl.String,
        "max_installment_months": pl.Int64,
    },
)

FINANCIAL_EVENTS = TableSchema(
    name="financial_events",
    file="financial_events.csv",
    dtypes={
        "event_id": pl.String,
        "user_id": pl.String,
        "event_type": pl.String,
        "description": pl.String,
        "category": pl.String,
        "direction": pl.String,
        "amount": pl.Float64,
        "currency": pl.String,
        "event_date": pl.Date,
        "settlement_date": pl.Date,
        "status": pl.String,
        "linked_event_id": pl.String,
        "flexibility": pl.String,
        "minimum_allowed_amount": pl.Float64,
    },
)

EXCHANGE_RATES = TableSchema(
    name="exchange_rates",
    file="exchange_rates.csv",
    dtypes={
        "rate_date": pl.Date,
        "from_currency": pl.String,
        "to_currency": pl.String,
        "rate": pl.Float64,
    },
)

REQUEST_COLUMNS = {
    "request_id": pl.String,
    "user_id": pl.String,
    "request_date": pl.Date,
    "request_type": pl.String,
    "requested_amount": pl.Float64,
    "desired_completion_date": pl.Date,
    "allows_partial_payment": pl.Boolean,
    "request_text": pl.String,
}

REQUESTS = TableSchema(
    name="requests",
    file="requests.csv",
    dtypes=dict(REQUEST_COLUMNS),
)

SAMPLE_REQUESTS = TableSchema(
    name="sample_requests",
    file="sample_requests.csv",
    dtypes={
        **REQUEST_COLUMNS,
        "amount_safe_to_pay": pl.Float64,
        "affordability_status": pl.String,
        "recommended_payment_method": pl.String,
        "payment_plan": pl.String,
        "earliest_date_for_full_payment": pl.Date,
        "spending_changes_needed": pl.String,
        "decision_explanation": pl.String,
    },
)

REQUEST_PAYMENT_OPTIONS = TableSchema(
    name="request_payment_options",
    file="request_payment_options.csv",
    dtypes={
        "payment_option_id": pl.String,
        "request_id": pl.String,
        "payment_method": pl.String,
        "payment_amount": pl.Float64,
        "number_of_payments": pl.Int64,
        "first_payment_date": pl.Date,
        "payment_frequency_days": pl.Int64,
        "financing_fee": pl.Float64,
        "total_payable_amount": pl.Float64,
    },
)

MESSAGES = TableSchema(
    name="messages",
    file="messages.csv",
    dtypes={
        "message_id": pl.String,
        "user_id": pl.String,
        "request_id": pl.String,
        "related_event_id": pl.String,
        "sent_at": pl.Datetime,
        "source_type": pl.String,
        "message_text": pl.String,
    },
)

IMAGES = TableSchema(
    name="images",
    file="images.csv",
    dtypes={
        "image_id": pl.String,
        "user_id": pl.String,
        "request_id": pl.String,
        "related_event_id": pl.String,
    },
)

TABLES: tuple[TableSchema, ...] = (
    FINANCIAL_PROFILES,
    FINANCIAL_EVENTS,
    EXCHANGE_RATES,
    REQUESTS,
    SAMPLE_REQUESTS,
    REQUEST_PAYMENT_OPTIONS,
    MESSAGES,
    IMAGES,
)

TABLES_BY_NAME: dict[str, TableSchema] = {t.name: t for t in TABLES}

EXCLUDED_FILES: frozenset[str] = frozenset({"output.csv"})
