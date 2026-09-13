"""Async orchestration for the image analyser agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import polars as pl

from ...connectors import ImageInput, OpenAIConnector
from ...ingest.models import FinancialEvent, validate_records
from ...ingest.pipeline import DEFAULT_DATASET_DIR, load_dataset
from ...ingest.store import InMemoryDataStore
from .enrichment import apply_updates, compute_updates
from .models import ImageAnalysisRecord, ImageAnalyserResult, image_analysis_frame
from .schema import get_response_model, load_system_prompt

IMAGE_SUBDIR = Path("media") / "images"
USER_PROMPT = (
    "Analyse the attached image and extract the financial event fields it "
    "evidences. Return the strict JSON object."
)
DEFAULT_CONCURRENCY = 4


@dataclass
class _AnalysisOutcome:
    """Internal pairing of an ``image_analysis`` row and its pending updates."""

    record: ImageAnalysisRecord
    updates: dict[str, Any] = field(default_factory=dict)


class ImageAnalyser:
    """Async agent that enriches financial events from evidence images.

    Each image is matched to its financial event through the ``related_event_id``
    and ``user_id`` columns of the ``images`` table. Only fields that are empty
    in the events frame are written from the (non-null) LLM extraction.
    """

    def __init__(
        self,
        store: InMemoryDataStore | None = None,
        connector: OpenAIConnector | None = None,
        dataset_dir: Path | str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._dataset_dir = (
            Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
        )
        self._store = store or load_dataset(self._dataset_dir)
        self._connector = connector
        self._concurrency = max(1, concurrency)
        self._system_prompt = load_system_prompt()
        self._response_model = get_response_model()

    @property
    def store(self) -> InMemoryDataStore:
        return self._store

    def _connector_or_default(self) -> OpenAIConnector:
        if self._connector is None:
            self._connector = OpenAIConnector()
        return self._connector

    def _image_path(self, image_id: str) -> Path:
        return self._dataset_dir / IMAGE_SUBDIR / f"{image_id}.png"

    async def _analyse_one(self, row: dict[str, Any]) -> _AnalysisOutcome:
        path = self._image_path(row["image_id"])
        record = ImageAnalysisRecord(
            image_id=row["image_id"],
            user_id=row["user_id"],
            request_id=row["request_id"],
            related_event_id=row["related_event_id"],
            image_path=str(path),
        )
        if not path.is_file():
            record.error = f"missing image file: {path}"
            return _AnalysisOutcome(record)

        try:
            analysis = await self._connector_or_default().generate_structured(
                USER_PROMPT,
                self._response_model,
                system=self._system_prompt,
                images=[ImageInput.from_path(path)],
            )
        except Exception as exc:  # noqa: BLE001 - record and continue the batch
            record.error = f"{type(exc).__name__}: {exc}"
            return _AnalysisOutcome(record)

        record = record.model_copy(update=analysis.model_dump())

        matches = self._store.table("financial_events").filter(
            (pl.col("event_id") == row["related_event_id"])
            & (pl.col("user_id") == row["user_id"])
        )
        if matches.height == 0:
            record.error = f"no financial event {row['related_event_id']!r} for user"
            return _AnalysisOutcome(record)

        event = matches.row(0, named=True)
        updates = compute_updates(analysis, event)
        record.matched_event_id = event["event_id"]
        record.matched = True
        record.updated_fields = "|".join(updates) if updates else None
        return _AnalysisOutcome(record, updates)

    async def _analyse_many(
        self, rows: list[dict[str, Any]]
    ) -> list[_AnalysisOutcome]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def guarded(row: dict[str, Any]) -> _AnalysisOutcome:
            async with semaphore:
                return await self._analyse_one(row)

        return list(await asyncio.gather(*(guarded(row) for row in rows)))

    async def run(self, image_ids: Sequence[str] | None = None) -> ImageAnalyserResult:
        """Analyse the selected images and enrich the events frame."""
        images = self._store.table("images")
        if image_ids is not None:
            images = images.filter(pl.col("image_id").is_in(list(image_ids)))
        rows = list(images.iter_rows(named=True))

        outcomes = await self._analyse_many(rows)
        updates_by_event: dict[str, dict[str, Any]] = {}
        for outcome in outcomes:
            if outcome.updates and outcome.record.matched_event_id is not None:
                updates_by_event.setdefault(
                    outcome.record.matched_event_id, {}
                ).update(outcome.updates)

        image_analysis = image_analysis_frame([out.record for out in outcomes])
        updated_events = apply_updates(
            self._store.table("financial_events"), updates_by_event
        )
        if updates_by_event:
            event_records = validate_records(updated_events, FinancialEvent)
            self._store.register(
                "financial_events", updated_events, event_records, overwrite=True
            )
        return ImageAnalyserResult(
            image_analysis=image_analysis,
            financial_events=updated_events,
            store=self._store,
        )


__all__ = ["DEFAULT_CONCURRENCY", "ImageAnalyser", "_AnalysisOutcome"]
