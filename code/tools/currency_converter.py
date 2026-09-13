"""Currency conversion tool backed by the Polars exchange-rate table."""

from __future__ import annotations

from collections import deque
from datetime import date
from typing import Optional

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from ..ingest.models import Currency


class ConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float
    from_currency: Currency
    to_currency: Currency
    rate_date: date


class ConversionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float
    from_currency: Currency
    to_currency: Currency
    converted_amount: float
    rate: float
    rate_date: date
    path: list[Currency] = Field(min_length=1)


class ConversionRateError(LookupError):
    """Raised when no rate path exists on or before the requested date."""


class CurrencyConverter:
    """Converts amounts between allowed currencies using dated exchange rates.

    Rates are looked up on the requested date, falling back to the most recent
    rate on or before it. Direct and inverse rates are used first, then the
    shortest cross-currency path (for example ZAR -> EUR -> USD -> INR).
    """

    ALLOWED = tuple(currency.value for currency in Currency)

    def __init__(self, exchange_rates: pl.DataFrame) -> None:
        required = {"rate_date", "from_currency", "to_currency", "rate"}
        missing = required - set(exchange_rates.columns)
        if missing:
            raise ValueError(f"exchange_rates missing columns: {sorted(missing)}")
        allowed = pl.Series("currency", list(self.ALLOWED))
        self._rates = exchange_rates.filter(
            pl.col("from_currency").is_in(allowed)
            & pl.col("to_currency").is_in(allowed)
        )

    @classmethod
    def from_store(cls, store: object) -> "CurrencyConverter":
        return cls(store.table("exchange_rates"))  # type: ignore[attr-defined]

    def convert(
        self,
        amount: float,
        from_currency: str,
        to_currency: str,
        rate_date: date | str,
    ) -> float:
        """Convert ``amount`` between two currency strings on ``rate_date``."""
        return self.convert_detailed(
            amount, from_currency, to_currency, rate_date
        ).converted_amount

    def convert_detailed(
        self,
        amount: float,
        from_currency: str,
        to_currency: str,
        rate_date: date | str,
    ) -> ConversionResult:
        normalized_rate_date = (
            date.fromisoformat(rate_date) if isinstance(rate_date, str) else rate_date
        )
        request = ConversionRequest(
            amount=amount,
            from_currency=Currency(from_currency),
            to_currency=Currency(to_currency),
            rate_date=normalized_rate_date,
        )
        if request.from_currency == request.to_currency:
            return ConversionResult(
                amount=request.amount,
                from_currency=request.from_currency,
                to_currency=request.to_currency,
                converted_amount=request.amount,
                rate=1.0,
                rate_date=request.rate_date,
                path=[request.from_currency],
            )

        graph = self._adjacency(request.rate_date)
        path, rate = self._shortest_path(
            graph, request.from_currency, request.to_currency
        )
        if path is None or rate is None:
            raise ConversionRateError(
                f"no rate path {request.from_currency.value}->"
                f"{request.to_currency.value} on or before {request.rate_date}"
            )
        return ConversionResult(
            amount=request.amount,
            from_currency=request.from_currency,
            to_currency=request.to_currency,
            converted_amount=request.amount * rate,
            rate=rate,
            rate_date=request.rate_date,
            path=path,
        )

    def _adjacency(self, on_date: date) -> dict[str, dict[str, float]]:
        latest = (
            self._rates.filter(pl.col("rate_date") <= on_date)
            .group_by(["from_currency", "to_currency"])
            .agg(pl.col("rate").sort_by("rate_date").last().alias("rate"))
        )
        graph: dict[str, dict[str, float]] = {code: {} for code in self.ALLOWED}
        direct: list[tuple[str, str, float]] = []
        for row in latest.iter_rows(named=True):
            source = row["from_currency"]
            target = row["to_currency"]
            rate = float(row["rate"])
            if rate > 0:
                direct.append((source, target, rate))
        for source, target, rate in direct:
            graph.setdefault(target, {}).setdefault(source, 1.0 / rate)
        for source, target, rate in direct:
            graph.setdefault(source, {})[target] = rate
        return graph

    @staticmethod
    def _shortest_path(
        graph: dict[str, dict[str, float]], source: Currency, target: Currency
    ) -> tuple[Optional[list[Currency]], Optional[float]]:
        start, goal = source.value, target.value
        queue: deque[tuple[str, list[str], float]] = deque([(start, [start], 1.0)])
        visited = {start}
        while queue:
            node, path, rate = queue.popleft()
            for neighbor in sorted(graph.get(node, {})):
                next_rate = rate * graph[node][neighbor]
                if neighbor == goal:
                    return [Currency(code) for code in path + [neighbor]], next_rate
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor], next_rate))
        return None, None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from ..ingest.pipeline import load_dataset

    parser = argparse.ArgumentParser(description="Convert between dataset currencies.")
    parser.add_argument("--amount", type=float, required=True)
    parser.add_argument("--from", dest="source", required=True, choices=CurrencyConverter.ALLOWED)
    parser.add_argument("--to", dest="target", required=True, choices=CurrencyConverter.ALLOWED)
    parser.add_argument("--date", required=True, help="rate date, YYYY-MM-DD")
    parser.add_argument("--dataset-dir", default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    store = load_dataset(args.dataset_dir)
    converter = CurrencyConverter.from_store(store)
    result = converter.convert_detailed(
        args.amount, args.source, args.target, args.date
    )
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

