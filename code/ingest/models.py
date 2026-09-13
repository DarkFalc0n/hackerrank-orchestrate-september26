"""Pydantic schemas and enums for validating the Buy or Wait? dataset."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

import polars as pl
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

BASE_CONFIG = ConfigDict(extra="forbid", use_enum_values=True)


class Currency(str, Enum):
    INR = "INR"
    ZAR = "ZAR"
    IDR = "IDR"
    USD = "USD"
    EUR = "EUR"


class Direction(str, Enum):
    credit = "credit"
    debit = "debit"
    non_cash = "non_cash"


class EventStatus(str, Enum):
    settled = "settled"
    pending = "pending"
    scheduled = "scheduled"
    failed = "failed"
    cancelled = "cancelled"
    unrealized = "unrealized"


class EventType(str, Enum):
    expense = "expense"
    income = "income"
    debt_payment = "debt_payment"
    subscription = "subscription"
    refund = "refund"
    investment_purchase = "investment_purchase"
    investment_sale = "investment_sale"
    investment_valuation = "investment_valuation"


class EventCategory(str, Enum):
    """Every category value observed in dataset/financial_events.csv."""

    cloud_storage = "cloud_storage"
    debt_repayment = "debt_repayment"
    delivery_membership = "delivery_membership"
    dining = "dining"
    education = "education"
    entertainment = "entertainment"
    family_support = "family_support"
    groceries = "groceries"
    gym = "gym"
    healthcare = "healthcare"
    housing = "housing"
    insurance = "insurance"
    investment = "investment"
    music_subscription = "music_subscription"
    rent = "rent"
    salary = "salary"
    shopping = "shopping"
    streaming = "streaming"
    transport = "transport"
    utilities = "utilities"
    windfall = "windfall"
    work_expense = "work_expense"


class Flexibility(str, Enum):
    fixed = "fixed"
    reducible = "reducible"
    stoppable = "stoppable"
    reducible_or_stoppable = "reducible_or_stoppable"


class PaymentMethod(str, Enum):
    full_payment = "full_payment"
    partial_payment = "partial_payment"
    installments = "installments"
    wait = "wait"
    not_recommended = "not_recommended"


class RequestType(str, Enum):
    purchase = "purchase"
    travel = "travel"
    education = "education"
    family_transfer = "family_transfer"
    debt_repayment = "debt_repayment"
    investment = "investment"
    housing = "housing"
    emergency_expense = "emergency_expense"
    other = "other"


class AffordabilityStatus(str, Enum):
    affordable_now = "affordable_now"
    affordable_with_plan = "affordable_with_plan"
    affordable_later = "affordable_later"
    not_affordable = "not_affordable"


class SourceType(str, Enum):
    bank = "bank"
    employer = "employer"
    financial_service = "financial_service"
    merchant = "merchant"
    service_provider = "service_provider"


class FinancialProfile(BaseModel):
    model_config = BASE_CONFIG

    user_id: str
    home_currency: Currency
    current_available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: str | None = None
    expense_categories_to_protect: str | None = None
    expense_categories_user_is_willing_to_reduce: str | None = None
    expense_categories_user_is_willing_to_stop: str | None = None
    payment_methods_user_will_consider: str | None = None
    max_installment_months: int | None = None

    @field_validator("payment_methods_user_will_consider")
    @classmethod
    def _validate_payment_methods(cls, value: str | None) -> str | None:
        if value is None:
            return value
        tokens = [token.strip() for token in value.split("|") if token.strip()]
        for token in tokens:
            PaymentMethod(token)
        return value


class FinancialEvent(BaseModel):
    model_config = BASE_CONFIG

    event_id: str
    user_id: str
    event_type: EventType
    description: str | None = None
    category: EventCategory | None = None
    direction: Direction
    amount: float | None = None
    currency: Currency
    event_date: date
    settlement_date: date | None = None
    status: EventStatus
    linked_event_id: str | None = None
    flexibility: Flexibility | None = None
    minimum_allowed_amount: float | None = None


class ExchangeRate(BaseModel):
    model_config = BASE_CONFIG

    rate_date: date
    from_currency: Currency
    to_currency: Currency
    rate: float = Field(gt=0)


class Request(BaseModel):
    model_config = BASE_CONFIG

    request_id: str
    user_id: str
    request_date: date
    request_type: RequestType
    requested_amount: float = Field(ge=0)
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


class SampleRequest(Request):
    amount_safe_to_pay: float | None = None
    affordability_status: AffordabilityStatus | None = None
    recommended_payment_method: PaymentMethod | None = None
    payment_plan: str | None = None
    earliest_date_for_full_payment: date | None = None
    spending_changes_needed: str | None = None
    decision_explanation: str | None = None


class RequestPaymentOption(BaseModel):
    model_config = BASE_CONFIG

    payment_option_id: str
    request_id: str
    payment_method: PaymentMethod
    payment_amount: float = Field(gt=0)
    number_of_payments: int | None = Field(default=None, ge=1)
    first_payment_date: date | None = None
    payment_frequency_days: int | None = Field(default=None, ge=0)
    financing_fee: float | None = Field(default=None, ge=0)
    total_payable_amount: float | None = Field(default=None, ge=0)


class Message(BaseModel):
    model_config = BASE_CONFIG

    message_id: str
    user_id: str
    request_id: str | None = None
    related_event_id: str | None = None
    sent_at: datetime
    source_type: SourceType
    message_text: str


class Image(BaseModel):
    model_config = BASE_CONFIG

    image_id: str
    user_id: str
    request_id: str | None = None
    related_event_id: str | None = None


class DatasetValidationError(ValueError):
    """Raised when ingested rows fail their Pydantic schema."""


TABLE_MODELS: dict[str, type[BaseModel]] = {
    "financial_profiles": FinancialProfile,
    "financial_events": FinancialEvent,
    "exchange_rates": ExchangeRate,
    "requests": Request,
    "sample_requests": SampleRequest,
    "request_payment_options": RequestPaymentOption,
    "messages": Message,
    "images": Image,
}


def model_for(table_name: str) -> type[BaseModel]:
    try:
        return TABLE_MODELS[table_name]
    except KeyError as exc:
        known = ", ".join(sorted(TABLE_MODELS))
        raise KeyError(f"no Pydantic model for '{table_name}' (known: {known})") from exc


def validate_records(
    frame: pl.DataFrame, model: type[BaseModel]
) -> list[BaseModel]:
    """Validate every row of ``frame`` against ``model``."""
    records: list[BaseModel] = []
    errors: list[str] = []
    for index, row in enumerate(frame.iter_rows(named=True)):
        try:
            records.append(model.model_validate(row))
        except ValidationError as exc:
            errors.append(_format_error(index, row, exc))
    if errors:
        raise DatasetValidationError(
            f"{model.__name__}: {len(errors)} invalid row(s)\n" + "\n".join(errors)
        )
    return records


def _format_error(index: int, row: dict[str, Any], exc: ValidationError) -> str:
    details = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )
    return f"- row {index}: {details}"
