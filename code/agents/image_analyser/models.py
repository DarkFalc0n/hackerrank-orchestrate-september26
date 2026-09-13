"""Pydantic models and dataframe schemas for the image analyser agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict

from ...ingest.store import InMemoryDataStore


class ImageAnalysisRecord(BaseModel):
    """One row of the ``image_analysis`` dataframe.

    The extraction fields mirror ``output_schema.jsonc`` (an image only
    evidences the missing transaction amount). ``matched_event_id``,
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
    amount: float | None = None
    direction: str | None = None

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
    "amount": pl.Float64,
    "direction": pl.String,
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
    usage: pl.DataFrame
    store: InMemoryDataStore
    cache_hits: int = 0
    cache_path: Path | None = None


__all__ = [
    "IMAGE_ANALYSIS_DTYPES",
    "ImageAnalysisRecord",
    "ImageAnalyserResult",
    "image_analysis_frame",
]
