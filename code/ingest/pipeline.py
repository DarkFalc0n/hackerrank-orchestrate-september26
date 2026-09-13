"""Ingestion pipeline that loads the dataset into an in-memory datastore."""

from __future__ import annotations

from pathlib import Path

import polars as pl
from pydantic import BaseModel

from .reader import read_table
from .schemas import EXCLUDED_FILES, TABLES, TABLES_BY_NAME, TableSchema
from .store import InMemoryDataStore

DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[2] / "dataset"


class IngestionPipeline:
    """Ingest every participant-facing CSV except ``output.csv``."""

    def __init__(self, dataset_dir: Path | str | None = None) -> None:
        self.dataset_dir = Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR

    def available_files(self) -> list[str]:
        return sorted(
            path.name
            for path in self.dataset_dir.glob("*.csv")
            if path.name not in EXCLUDED_FILES
        )

    def ingest_table(self, schema: TableSchema) -> tuple[pl.DataFrame, list[BaseModel]]:
        path = self.dataset_dir / schema.file
        if not path.exists():
            raise FileNotFoundError(f"missing dataset file: {path}")
        return read_table(self.dataset_dir, schema)

    def run(self, tables: list[str] | None = None) -> InMemoryDataStore:
        store = InMemoryDataStore()
        selected = tables if tables is not None else [t.name for t in TABLES]
        for name in selected:
            try:
                schema = TABLES_BY_NAME[name]
            except KeyError as exc:
                known = ", ".join(sorted(TABLES_BY_NAME))
                raise KeyError(f"unknown table '{name}' (known: {known})") from exc
            frame, records = self.ingest_table(schema)
            store.register(name, frame, records)
        return store


def load_dataset(dataset_dir: Path | str | None = None) -> InMemoryDataStore:
    """Convenience entry point: ingest the full dataset and return the store."""
    return IngestionPipeline(dataset_dir).run()
