"""Robust CSV reading and Polars type casting helpers."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import polars as pl
from pydantic import BaseModel

from .models import model_for, validate_records
from .schemas import TableSchema

_DATE_FORMAT = "%Y-%m-%d"
_TRUE_VALUES = ("true", "1", "yes", "y")
_FALSE_VALUES = ("false", "0", "no", "n")


def _read_with_polars(path: Path) -> pl.DataFrame:
    return pl.read_csv(path, infer_schema_length=0)


def _read_with_csv_module(path: Path) -> pl.DataFrame:
    """Fallback for quoted fields padded with leading/trailing spaces.

    Several supplied files place a space between the delimiter and an opening
    quote (`` , "text"``), which is not valid CSV quoting. Python's csv reader
    with ``skipinitialspace`` tokenizes those rows correctly.
    """
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    rows = list(reader)
    if not rows:
        return pl.DataFrame()

    header = [_clean_name(name) for name in rows[0]]
    width = len(header)
    normalized = []
    for row in rows[1:]:
        if len(row) < width:
            row = row + [""] * (width - len(row))
        normalized.append(row[:width])
    return pl.DataFrame(normalized, schema=header, orient="row")


def read_raw(path: Path) -> pl.DataFrame:
    """Read a CSV into an all-String Polars frame, preserving missing values."""
    try:
        df = _read_with_polars(path)
    except pl.exceptions.ComputeError:
        df = _read_with_csv_module(path)

    df = df.rename({name: _clean_name(name) for name in df.columns})
    df = df.with_columns(pl.all().cast(pl.String).str.strip_chars())
    df = df.with_columns(
        [
            pl.when(pl.col(name) == "").then(None).otherwise(pl.col(name)).alias(name)
            for name in df.columns
        ]
    )
    return df


def _clean_name(name: str) -> str:
    return name.lstrip("\ufeff").strip()


def _cast_expr(name: str, dtype: pl.DataType | type[pl.DataType]) -> pl.Expr:
    col = pl.col(name)
    if dtype == pl.Date:
        return col.str.to_date(format=_DATE_FORMAT, strict=False).alias(name)
    if dtype == pl.Datetime:
        return col.str.to_datetime(
            format="%Y-%m-%dT%H:%M:%SZ", strict=False, time_zone="UTC"
        ).alias(name)
    if dtype == pl.Boolean:
        lowered = col.str.to_lowercase()
        return (
            pl.when(lowered.is_in(_TRUE_VALUES))
            .then(True)
            .when(lowered.is_in(_FALSE_VALUES))
            .then(False)
            .otherwise(None)
            .alias(name)
        )
    if dtype == pl.Int64:
        return col.cast(pl.Float64, strict=False).cast(pl.Int64).alias(name)
    return col.cast(dtype, strict=False).alias(name)


def cast_frame(df: pl.DataFrame, schema: TableSchema) -> pl.DataFrame:
    """Validate columns and cast an all-String frame to the schema dtypes."""
    expected = set(schema.columns)
    actual = set(df.columns)
    if expected != actual:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            f"{schema.name}: schema mismatch (missing={missing}, unexpected={unexpected})"
        )
    return df.select([_cast_expr(name, dtype) for name, dtype in schema.dtypes.items()])


def read_table(
    dataset_dir: Path, schema: TableSchema
) -> tuple[pl.DataFrame, list[BaseModel]]:
    """Ingest one table: read raw strings, trim, cast, and validate with Pydantic."""
    raw = read_raw(dataset_dir / schema.file)
    frame = cast_frame(raw, schema)
    records = validate_records(frame, model_for(schema.name))
    return frame, records
