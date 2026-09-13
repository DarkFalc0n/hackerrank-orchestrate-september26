"""Pydantic models and dataframe schemas for the image analyser agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict

from ...ingest.store import InMemoryDataStore


class ImageAnalysisRecord(BaseModel):
    """One row of the ``image_analysis`` dataframe.

    The extraction fields mirror ``output_schema.jsonc``. ``matched_event_id``,
    ``updated_fields``, and ``error`` describe how the extraction was applied to
    the financial-events table.
    """

    model_config = ConfigDict(extra="forbid")

    image_id: str
    user_id: str
    request_id: str | None = None
    related_event_id: str | None = None
    image_path: str

    summary: str | None = None
    event_type: str | None = None
    category: str | None = None
    direction: str | None = None
    amount: float | None = None
    currency: str | None = None
    event_date: date | None = None
    settlement_date: date | None = None
    status: str | None = None

    matched_event_id: str | None = None
    matched: bool = False
    updated_fields: str | None = None
    error: str | None = None


IMAGE_ANALYSIS_DTYPES: dict[str, Any] = {
    "image_id": pl.String,
    "user_id": pl.String,
    "request_id": pl.String,
    "related_event_id": pl.String,
    "image_path": pl.String,
    "summary": pl.String,
    "event_type": pl.String,
    "category": pl.String,
    "direction": pl.String,
    "amount": pl.Float64,
    "currency": pl.String,
    "event_date": pl.Date,
    "settlement_date": pl.Date,
    "status": pl.String,
    "matched_event_id": pl.String,
    "matched": pl.Boolean,
    "updated_fields": pl.String,
    "error": pl.String,
}


def image_analysis_frame(records: list[ImageAnalysisRecord]) -> pl.DataFrame:
    """Build the typed ``image_analysis`` dataframe from agent records."""
    return pl.DataFrame(
        [record.model_dump() for record in records],
        schema=IMAGE_ANALYSIS_DTYPES,
        orient="row",
    )


@dataclass
class ImageAnalyserResult:
    """Outcome of an image-analyser run."""

    image_analysis: pl.DataFrame
    financial_events: pl.DataFrame
    store: InMemoryDataStore


__all__ = [
    "IMAGE_ANALYSIS_DTYPES",
    "ImageAnalysisRecord",
    "ImageAnalyserResult",
    "image_analysis_frame",
]
