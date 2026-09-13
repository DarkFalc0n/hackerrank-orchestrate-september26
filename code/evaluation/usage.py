"""Token-usage accounting and ``usage_report.md`` generation.

Every LLM call in the pipeline records one row here. The report required by the
challenge lives at ``evaluation/usage_report.md`` and summarizes providers and
model names, call counts, input/output tokens, totals, per-request averages, and
an estimated cost. No credentials are ever written.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

AGENT_IMAGE = "image_analyser"
AGENT_MESSAGE = "message_analyser"
AGENT_REQUEST = "request_analyser"

PROVIDER = "OpenAI-compatible endpoint"

# Estimated USD per 1,000,000 tokens as (input, output). Extend as needed.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
}

USAGE_DTYPES: dict[str, Any] = {
    "agent": pl.String,
    "request_id": pl.String,
    "model": pl.String,
    "prompt_tokens": pl.Int64,
    "completion_tokens": pl.Int64,
    "total_tokens": pl.Int64,
}


def usage_row(
    agent: str,
    model: str,
    usage: Any,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build one usage row from a connector :class:`UsageStats` (or ``None``)."""
    return {
        "agent": agent,
        "request_id": request_id,
        "model": model,
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def usage_frame(rows: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    """Build the typed usage dataframe."""
    return pl.DataFrame(list(rows), schema=USAGE_DTYPES, orient="row")


def _fallback_rates() -> tuple[float, float] | None:
    """Optional env-configured rates for models absent from :data:`MODEL_PRICES`."""
    prompt = os.environ.get("USAGE_INPUT_PRICE_PER_M")
    completion = os.environ.get("USAGE_OUTPUT_PRICE_PER_M")
    if prompt and completion:
        return (float(prompt), float(completion))
    return None


def _estimate_cost(model: str, prompt: int, completion: int) -> float | None:
    prices = MODEL_PRICES.get(model) or _fallback_rates()
    if prices is None:
        return None
    input_rate, output_rate = prices
    return (prompt / 1_000_000) * input_rate + (completion / 1_000_000) * output_rate


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def write_usage_report(
    path: Path | str,
    usage: pl.DataFrame,
    request_count: int,
    *,
    dataset_dir: Path | str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> Path:
    """Write ``usage_report.md`` summarizing the full-dataset run."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    total_prompt = int(usage["prompt_tokens"].sum()) if usage.height else 0
    total_completion = int(usage["completion_tokens"].sum()) if usage.height else 0
    total_tokens = int(usage["total_tokens"].sum()) if usage.height else 0
    calls = usage.height

    lines: list[str] = ["# Token Usage and Cost Report", ""]
    lines.append(f"- Provider(s): {PROVIDER}")
    if dataset_dir is not None:
        lines.append(f"- Dataset: `{dataset_dir}`")
    lines.append(f"- Requests: {request_count}")
    if started_at is not None:
        lines.append(f"- Started: {started_at.isoformat(timespec='seconds')}")
    if finished_at is not None:
        lines.append(f"- Finished: {finished_at.isoformat(timespec='seconds')}")
    lines.append(f"- LLM calls: {calls}")
    lines.append("")

    if usage.height:
        models = sorted(set(usage["model"].drop_nulls().to_list()))
        lines.append(f"- Model(s): {', '.join(models) if models else 'n/a'}")
        lines.append("")

        # Per-agent and per-model breakdown.
        rows: list[list[str]] = []
        group = (
            usage.group_by(["agent", "model"])
            .agg(
                pl.len().alias("calls"),
                pl.col("prompt_tokens").sum().alias("prompt_tokens"),
                pl.col("completion_tokens").sum().alias("completion_tokens"),
                pl.col("total_tokens").sum().alias("total_tokens"),
            )
            .sort(["agent", "model"])
        )
        cost_total = 0.0
        priced = False
        for row in group.iter_rows(named=True):
            cost = _estimate_cost(
                row["model"], row["prompt_tokens"], row["completion_tokens"]
            )
            if cost is not None:
                cost_total += cost
                priced = True
            rows.append(
                [
                    str(row["agent"]),
                    str(row["model"]),
                    str(row["calls"]),
                    str(row["prompt_tokens"]),
                    str(row["completion_tokens"]),
                    str(row["total_tokens"]),
                    f"${cost:.4f}" if cost is not None else "n/a",
                ]
            )
        lines.append("## Per-agent and per-model usage")
        lines.append("")
        lines.append(
            _md_table(
                [
                    "Agent",
                    "Model",
                    "Calls",
                    "Input tokens",
                    "Output tokens",
                    "Total tokens",
                    "Est. cost",
                ],
                rows,
            )
        )
        lines.append("")

        lines.append("## Totals")
        lines.append("")
        lines.append(
            _md_table(
                ["Metric", "Value"],
                [
                    ["Total calls", str(calls)],
                    ["Total input tokens", str(total_prompt)],
                    ["Total output tokens", str(total_completion)],
                    ["Total tokens", str(total_tokens)],
                    [
                        "Average input tokens / request",
                        f"{(total_prompt / request_count):.2f}"
                        if request_count
                        else "n/a",
                    ],
                    [
                        "Average output tokens / request",
                        f"{(total_completion / request_count):.2f}"
                        if request_count
                        else "n/a",
                    ],
                    [
                        "Average total tokens / request",
                        f"{(total_tokens / request_count):.2f}"
                        if request_count
                        else "n/a",
                    ],
                    [
                        "Estimated total cost (USD)",
                        f"${cost_total:.4f}" if priced else "n/a",
                    ],
                    [
                        "Estimated cost per request (USD)",
                        f"${(cost_total / request_count):.6f}"
                        if priced and request_count
                        else "n/a",
                    ],
                ],
            )
        )
        lines.append("")
        lines.append(
            "Cost estimate uses published per-1M-token rates for the listed "
            "models; verify against your provider's current pricing."
        )
    else:
        lines.append("No LLM calls were recorded for this run.")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


__all__ = [
    "AGENT_IMAGE",
    "AGENT_MESSAGE",
    "AGENT_REQUEST",
    "MODEL_PRICES",
    "PROVIDER",
    "USAGE_DTYPES",
    "usage_frame",
    "usage_row",
    "write_usage_report",
]
