"""Async orchestration for the image analyser agent."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import polars as pl

from ...connectors import ImageInput, OpenAIConnector
from ...evaluation.usage import AGENT_IMAGE, usage_frame, usage_row
from ...ingest.models import FinancialEvent, validate_records
from ...ingest.pipeline import DEFAULT_DATASET_DIR, load_dataset
from ...ingest.store import InMemoryDataStore
from .enrichment import apply_updates, compute_updates
from .models import ImageAnalysisRecord, ImageAnalyserResult, image_analysis_frame
from .schema import get_response_model, load_system_prompt

IMAGE_SUBDIR = Path("media") / "images"
USER_PROMPT = (
    "Analyse the attached image and extract the missing amount evidenced for the "
    "financial event described in EVENT_CONTEXT. Return the strict JSON object."
)
DEFAULT_CONCURRENCY = 4

_EVENT_CONTEXT_KEYS = (
    "event_id",
    "event_type",
    "category",
    "description",
    "direction",
    "currency",
    "event_date",
    "settlement_date",
    "status",
)

# Successful extractions are cached here so a re-run does not pay for the same
# image again. Pass ``--force`` (or delete the file) to ignore the cache.
DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "cache" / "image_analysis.json"
)


@dataclass
class _AnalysisOutcome:
    """Internal pairing of an ``image_analysis`` row and its pending updates."""

    record: ImageAnalysisRecord
    updates: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] | None = None


class ImageAnalyser:
    """Async agent that enriches financial events from evidence images.

    Each image is matched to its financial event through the ``related_event_id``
    and ``user_id`` columns of the ``images`` table, and only the event's missing
    ``amount`` is written from the (non-null) LLM extraction. Extractions are
    cached as JSON; set ``force=True`` (or delete the cache file) to re-run the
    model for every image.
    """

    def __init__(
        self,
        store: InMemoryDataStore | None = None,
        connector: OpenAIConnector | None = None,
        dataset_dir: Path | str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        cache_path: Path | str | None = None,
        force: bool = False,
    ) -> None:
        self._dataset_dir = (
            Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
        )
        self._store = store or load_dataset(self._dataset_dir)
        self._connector = connector
        self._concurrency = max(1, concurrency)
        self._cache_path = (
            Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
        )
        self._force = force
        self._system_prompt = load_system_prompt()
        self._response_model = get_response_model()

    @property
    def store(self) -> InMemoryDataStore:
        return self._store

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def _connector_or_default(self) -> OpenAIConnector:
        if self._connector is None:
            self._connector = OpenAIConnector()
        return self._connector

    def _image_path(self, image_id: str) -> Path:
        return self._dataset_dir / IMAGE_SUBDIR / f"{image_id}.png"

    @staticmethod
    def _new_record(row: dict[str, Any], path: Path) -> ImageAnalysisRecord:
        return ImageAnalysisRecord(
            image_id=row["image_id"],
            user_id=row["user_id"],
            request_id=row["request_id"],
            related_event_id=row["related_event_id"],
            image_path=str(path),
        )

    def _with_analysis(
        self,
        row: dict[str, Any],
        record: ImageAnalysisRecord,
        analysis: Any,
    ) -> _AnalysisOutcome:
        """Match an extraction to its event and compute the field updates."""
        record = record.model_copy(update=analysis.model_dump())
        matches = self._store.table("financial_events").filter(
            (pl.col("event_id") == row["related_event_id"])
            & (pl.col("user_id") == row["user_id"])
        )
        if matches.height == 0:
            record.error = f"no financial event {row['related_event_id']!r} for user"
            return _AnalysisOutcome(record)
        event = matches.row(0, named=True)
        evidenced = getattr(analysis, "direction", None)
        if evidenced not in (None, "", event["direction"]):
            # The image evidences the opposite cash direction (for example a
            # receipt for money received against a debit row), so it does not
            # confirm this event's amount. Keep the match for the audit trail
            # but write nothing.
            record.matched_event_id = event["event_id"]
            record.matched = True
            record.updated_fields = None
            return _AnalysisOutcome(record)
        updates = compute_updates(analysis, event)
        record.matched_event_id = event["event_id"]
        record.matched = True
        record.updated_fields = "|".join(updates) if updates else None
        return _AnalysisOutcome(record, updates)

    def _event_context(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Return the financial event this image should evidence, as JSON data."""
        matches = self._store.table("financial_events").filter(
            (pl.col("event_id") == row["related_event_id"])
            & (pl.col("user_id") == row["user_id"])
        )
        if matches.height == 0:
            return None
        event = matches.row(0, named=True)
        return {
            key: (
                value.isoformat()
                if hasattr(value, "isoformat")
                else value
            )
            for key, value in event.items()
            if key in _EVENT_CONTEXT_KEYS
        }

    async def _analyse_one(self, row: dict[str, Any]) -> _AnalysisOutcome:
        path = self._image_path(row["image_id"])
        record = self._new_record(row, path)
        if not path.is_file():
            record.error = f"missing image file: {path}"
            return _AnalysisOutcome(record)

        context = self._event_context(row)
        prompt = (
            f"{USER_PROMPT}\n\n"
            f"EVENT_CONTEXT:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n"
            if context is not None
            else USER_PROMPT
        )
        try:
            result = await self._connector_or_default().generate_structured_detailed(
                prompt,
                self._response_model,
                system=self._system_prompt,
                images=[ImageInput.from_path(path)],
            )
        except Exception as exc:  # noqa: BLE001 - record and continue the batch
            record.error = f"{type(exc).__name__}: {exc}"
            return _AnalysisOutcome(record)

        outcome = self._with_analysis(row, record, result.parsed)
        outcome.usage = usage_row(
            AGENT_IMAGE, result.model, result.usage, row["request_id"]
        )
        return outcome

    def _cached_outcome(
        self, row: dict[str, Any], entry: dict[str, Any]
    ) -> _AnalysisOutcome:
        """Rebuild an outcome from a cached extraction, without an LLM call."""
        record = self._new_record(row, self._image_path(row["image_id"]))
        analysis = self._response_model.model_validate(
            {
                "summary": entry.get("summary"),
                "amount": entry.get("amount"),
                "direction": entry.get("direction"),
            }
        )
        return self._with_analysis(row, record, analysis)

    def _read_cache(self) -> dict[str, dict[str, Any]]:
        if not self._cache_path.is_file():
            return {}
        try:
            payload = json.loads(
                self._cache_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return {}
        images = payload.get("images") if isinstance(payload, dict) else None
        return images if isinstance(images, dict) else {}

    def _write_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(
                {"version": 1, "images": cache},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    async def _analyse_many(
        self,
        rows: list[dict[str, Any]],
        progress: Callable[[int, int], None] | None = None,
    ) -> list[_AnalysisOutcome]:
        semaphore = asyncio.Semaphore(self._concurrency)
        total = len(rows)
        done = 0

        async def guarded(row: dict[str, Any]) -> _AnalysisOutcome:
            nonlocal done
            async with semaphore:
                outcome = await self._analyse_one(row)
            done += 1
            if progress is not None:
                progress(done, total)
            return outcome

        return list(await asyncio.gather(*(guarded(row) for row in rows)))

    async def run(
        self,
        image_ids: Sequence[str] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> ImageAnalyserResult:
        """Analyse the selected images and enrich the events frame.

        Cached extractions are reused unless ``force`` is set; new successful
        extractions are merged back into the cache file.
        """
        images = self._store.table("images")
        if image_ids is not None:
            images = images.filter(pl.col("image_id").is_in(list(image_ids)))
        rows = list(images.iter_rows(named=True))

        cache = {} if self._force else self._read_cache()
        outcomes: list[_AnalysisOutcome | None] = [None] * len(rows)
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, row in enumerate(rows):
            entry = cache.get(row["image_id"])
            if entry is not None:
                outcomes[index] = self._cached_outcome(row, entry)
            else:
                pending.append((index, row))

        if pending:
            fresh = await self._analyse_many(
                [row for _, row in pending], progress
            )
            for (index, _), outcome in zip(pending, fresh):
                outcomes[index] = outcome

        resolved = [outcome for outcome in outcomes if outcome is not None]
        usage_rows = [
            outcome.usage
            for outcome in resolved
            if outcome.usage is not None
        ]

        updated_cache = dict(cache)
        for outcome in resolved:
            record = outcome.record
            if record.error is None and (
                record.amount is not None or record.summary
            ):
                updated_cache[record.image_id] = {
                    "summary": record.summary,
                    "amount": record.amount,
                    "direction": record.direction,
                }
        if pending:
            self._write_cache(updated_cache)

        updates_by_event: dict[str, dict[str, Any]] = {}
        for outcome in resolved:
            if outcome.updates and outcome.record.matched_event_id is not None:
                updates_by_event.setdefault(
                    outcome.record.matched_event_id, {}
                ).update(outcome.updates)

        image_analysis = image_analysis_frame([outcome.record for outcome in resolved])
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
            usage=usage_frame(usage_rows),
            store=self._store,
            cache_hits=len(rows) - len(pending),
            cache_path=self._cache_path,
        )


__all__ = [
    "DEFAULT_CACHE_PATH",
    "DEFAULT_CONCURRENCY",
    "ImageAnalyser",
    "_AnalysisOutcome",
]
