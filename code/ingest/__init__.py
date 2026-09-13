"""Polars-based ingestion pipelines for the Buy or Wait? dataset."""

from .models import (
    AffordabilityStatus,
    Currency,
    DatasetValidationError,
    Direction,
    EventCategory,
    EventStatus,
    Flexibility,
    PaymentMethod,
    PaymentMethodOption,
    RequestType,
    TABLE_MODELS,
    model_for,
    validate_records,
)
from .pipeline import IngestionPipeline, load_dataset
from .schemas import TABLES, TABLES_BY_NAME, TableSchema
from .store import InMemoryDataStore

__all__ = [
    "AffordabilityStatus",
    "Currency",
    "DatasetValidationError",
    "Direction",
    "EventCategory",
    "EventStatus",
    "Flexibility",
    "IngestionPipeline",
    "InMemoryDataStore",
    "PaymentMethod",
    "PaymentMethodOption",
    "RequestType",
    "TABLE_MODELS",
    "TABLES",
    "TABLES_BY_NAME",
    "TableSchema",
    "load_dataset",
    "model_for",
    "validate_records",
]
