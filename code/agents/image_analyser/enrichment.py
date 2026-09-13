"""Pure helpers that decide and apply financial-event field updates."""

from __future__ import annotations

from typing import Any, Mapping

import polars as pl
from pydantic import BaseModel

UPDATABLE_FIELDS: tuple[str, ...] = (
    "event_type",
    "category",
    "direction",
    "amount",
    "currency",
    "event_date",
    "settlement_date",
    "status",
)


def compute_updates(
    analysis: BaseModel, event: Mapping[str, Any]
) -> dict[str, Any]:
    """Return non-null LLM values for fields that are empty in ``event``."""
    updates: dict[str, Any] = {}
    for field in UPDATABLE_FIELDS:
        value = getattr(analysis, field, None)
        if value is not None and event.get(field) is None:
            updates[field] = value
    return updates


def apply_updates(
    events: pl.DataFrame, updates_by_event: Mapping[str, Mapping[str, Any]]
) -> pl.DataFrame:
    """Return the events frame with per-event field updates applied."""
    result = events
    for event_id, updates in updates_by_event.items():
        if not updates:
            continue
        exprs = [
            pl.when(pl.col("event_id") == event_id)
            .then(pl.lit(value, dtype=result.schema[field]))
            .otherwise(pl.col(field))
            .alias(field)
            for field, value in updates.items()
        ]
        result = result.with_columns(exprs)
    return result


__all__ = ["UPDATABLE_FIELDS", "apply_updates", "compute_updates"]
