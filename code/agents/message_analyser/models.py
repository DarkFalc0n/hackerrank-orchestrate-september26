"""Pydantic models and dataframe schemas for the message analyser agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict

from ...ingest.store import InMemoryDataStore
from ...tools.forecasting import ForecastLedger


class MessageAnalysisRecord(BaseModel):
    """One row of the ``message_analysis`` dataframe.

    The leading fields echo the input ``messages`` row. ``tool``, ``reasoning``,
    and ``confidence`` are the LLM decision; ``params_json`` is the validated
    tool-call arguments; ``applied``/``affected``/``error`` describe whether the
    ledger accepted the call.
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str
    user_id: str
    request_id: str | None = None
    related_event_id: str | None = None
    sent_at: datetime | None = None
    source_type: str | None = None

    call_index: int = 0
    tool: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    params_json: str | None = None

    ledger_request_id: str | None = None
    applied: bool = False
    affected: int | None = None
    error: str | None = None


MESSAGE_ANALYSIS_DTYPES: dict[str, Any] = {
    "message_id": pl.String,
    "user_id": pl.String,
    "request_id": pl.String,
    "related_event_id": pl.String,
    "sent_at": pl.Datetime(time_unit="us", time_zone="UTC"),
    "source_type": pl.String,
    "call_index": pl.Int64,
    "tool": pl.String,
    "reasoning": pl.String,
    "confidence": pl.Float64,
    "params_json": pl.String,
    "ledger_request_id": pl.String,
    "applied": pl.Boolean,
    "affected": pl.Int64,
    "error": pl.String,
}


def message_analysis_frame(records: list[MessageAnalysisRecord]) -> pl.DataFrame:
    """Build the typed ``message_analysis`` dataframe from agent records."""
    return pl.DataFrame(
        [record.model_dump() for record in records],
        schema=MESSAGE_ANALYSIS_DTYPES,
        orient="row",
    )


@dataclass
class MessageAnalyserResult:
    """Outcome of a message-analyser run."""

    message_analysis: pl.DataFrame
    ledgers: dict[str, ForecastLedger] = field(default_factory=dict)
    store: InMemoryDataStore | None = None


__all__ = [
    "MESSAGE_ANALYSIS_DTYPES",
    "MessageAnalysisRecord",
    "MessageAnalyserResult",
    "message_analysis_frame",
]
