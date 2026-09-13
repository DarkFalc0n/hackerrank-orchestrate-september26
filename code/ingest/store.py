"""In-memory datastore backed by Polars frames."""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel


class InMemoryDataStore:
    """A named collection of Polars DataFrames and validated Pydantic records."""

    def __init__(self) -> None:
        self._frames: dict[str, pl.DataFrame] = {}
        self._records: dict[str, list[BaseModel]] = {}

    def register(
        self,
        name: str,
        frame: pl.DataFrame,
        records: list[BaseModel] | None = None,
        overwrite: bool = False,
    ) -> None:
        if name in self._frames and not overwrite:
            raise KeyError(f"table already registered: {name}")
        self._frames[name] = frame
        self._records[name] = list(records) if records is not None else []

    def table(self, name: str) -> pl.DataFrame:
        try:
            return self._frames[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._frames)) or "<empty>"
            raise KeyError(f"unknown table '{name}' (known: {known})") from exc

    def records(self, name: str) -> list[BaseModel]:
        try:
            return self._records[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._records)) or "<empty>"
            raise KeyError(f"unknown table '{name}' (known: {known})") from exc

    def lazy(self, name: str) -> pl.LazyFrame:
        return self.table(name).lazy()

    def keys(self) -> list[str]:
        return list(self._frames)

    def __contains__(self, name: object) -> bool:
        return name in self._frames

    def __getitem__(self, name: str) -> pl.DataFrame:
        return self.table(name)

    def __len__(self) -> int:
        return len(self._frames)

    def summary(self) -> pl.DataFrame:
        rows = [
            {
                "table": name,
                "rows": frame.height,
                "columns": frame.width,
                "validated": len(self._records.get(name, [])),
            }
            for name, frame in self._frames.items()
        ]
        return pl.DataFrame(
            rows,
            schema={
                "table": pl.String,
                "rows": pl.Int64,
                "columns": pl.Int64,
                "validated": pl.Int64,
            },
            orient="row",
        )
