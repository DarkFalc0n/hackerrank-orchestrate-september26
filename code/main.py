"""Buy or Wait? — full end-to-end pipeline.

Flow (all stages live here in order):

1. Ingestion — load and validate every CSV in ``dataset/``.
2. Image analysis — enrich blank financial-event fields from evidence images.
3. Cash forecaster — build each request's base 90-day forecast ledger.
4. Message analysis — apply message evidence to the ledgers.
5. Scheduled-event resolution — engine resolves pending/scheduled events.
6. Payment plans — engine generates and ranks the candidate plans.
7. Request analyser — the LLM weighs the plans and writes ``output.csv``.

A token-usage and cost report is written for the complete run. Every stage logs
its start, progress, and completion so a long run is observable.

Usage:
    python -m code.main                      # full run: users user_026+
    python -m code.main --sample             # sample run: users user_01..user_025
    python -m code.main --requests request_26 request_27
    python -m code.main --skip-images --skip-messages --concurrency 6
    python -m code.main --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import polars as pl

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from code.agents.cash_forecaster import CashForecaster
    from code.agents.image_analyser import ImageAnalyser
    from code.agents.message_analyser import MessageAnalyser
    from code.agents.request_analyser import RequestAnalyser
    from code.engine import PaymentPlanEngine, plan_options_frame
    from code.engine.resolve_events import resolve_pending_scheduled_events
    from code.evaluation.usage import usage_frame, write_usage_report
    from code.ingest.pipeline import DEFAULT_DATASET_DIR, load_dataset
    from code.tools import CurrencyConverter
else:
    from .agents.cash_forecaster import CashForecaster
    from .agents.image_analyser import ImageAnalyser
    from .agents.message_analyser import MessageAnalyser
    from .agents.request_analyser import RequestAnalyser
    from .engine import PaymentPlanEngine, plan_options_frame
    from .engine.resolve_events import resolve_pending_scheduled_events
    from .evaluation.usage import usage_frame, write_usage_report
    from .ingest.pipeline import DEFAULT_DATASET_DIR, load_dataset
    from .tools import CurrencyConverter

USAGE_REPORT_PATH = Path(__file__).resolve().parent / "evaluation" / "usage_report.md"
SAMPLE_USAGE_REPORT_PATH = (
    Path(__file__).resolve().parent / "evaluation" / "sample_usage_report.md"
)
LOGGER = logging.getLogger("buyorwait")
TOTAL_STEPS = 7

# The request fields the pipeline actually consumes. Ground-truth columns on
# ``sample_requests`` are deliberately dropped so they can never leak in.
CORE_REQUEST_COLUMNS: tuple[str, ...] = (
    "request_id",
    "user_id",
    "request_date",
    "request_type",
    "requested_amount",
    "desired_completion_date",
    "allows_partial_payment",
    "request_text",
)

USER_SCOPED_TABLES: tuple[str, ...] = (
    "financial_profiles",
    "financial_events",
    "messages",
    "images",
)


def configure_logging(level: int = logging.INFO) -> None:
    """Route pipeline logs to stdout with timestamps."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def _apply_scope(
    store,
    users: Sequence[str],
    request_ids: Sequence[str],
) -> None:
    """Restrict every user-scoped table to the in-scope users and requests.

    Nothing outside the chosen user range is processed afterwards, so the sample
    run never touches users beyond ``user_025`` and the normal run starts at
    ``user_026``.
    """
    user_list = list(users)
    request_list = list(request_ids)
    for name in USER_SCOPED_TABLES:
        frame = store.table(name)
        store.register(
            name,
            frame.filter(pl.col("user_id").is_in(user_list)),
            [],
            overwrite=True,
        )
    options = store.table("request_payment_options")
    store.register(
        "request_payment_options",
        options.filter(pl.col("request_id").is_in(request_list)),
        [],
        overwrite=True,
    )


def progress_logger(label: str, *, every: int = 10) -> Callable[[int, int], None]:
    """Return a progress callback that logs every ``every`` items and the last."""

    def report(done: int, total: int) -> None:
        if total == 0 or done == 1 or done == total or done % every == 0:
            LOGGER.info("    %s: %d/%d", label, done, total)

    return report


def _request_ids(frame: pl.DataFrame, request_ids: Sequence[str] | None) -> list[str]:
    """Return the selected request IDs in the dataset's original order."""
    if request_ids is not None:
        frame = frame.filter(pl.col("request_id").is_in(list(request_ids)))
    return frame["request_id"].to_list()


async def run_pipeline(
    dataset_dir: Path | str | None = None,
    output_path: Path | str | None = None,
    usage_report_path: Path | str | None = None,
    request_ids: Sequence[str] | None = None,
    run_images: bool = True,
    run_messages: bool = True,
    sample: bool = False,
    concurrency: int = 4,
    force_images: bool = False,
    image_cache: Path | str | None = None,
    log_level: int = logging.INFO,
):
    """Run the seven stages and write ``output.csv`` and the usage report."""
    configure_logging(log_level)
    started_at = datetime.now()
    dataset_dir = Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
    requests_table = "sample_requests" if sample else "requests"
    output_path = (
        Path(output_path)
        if output_path
        else dataset_dir / ("sample_output.csv" if sample else "output.csv")
    )
    usage_report_path = (
        Path(usage_report_path)
        if usage_report_path
        else (SAMPLE_USAGE_REPORT_PATH if sample else USAGE_REPORT_PATH)
    )

    LOGGER.info("================================================================")
    LOGGER.info("Buy or Wait? pipeline start")
    LOGGER.info("mode        : %s", "SAMPLE (users user_01..user_025)" if sample else "FULL (users user_026+)")
    LOGGER.info("dataset dir : %s", dataset_dir)
    LOGGER.info("output      : %s", output_path)
    LOGGER.info("usage report: %s", usage_report_path)
    LOGGER.info("images=%s messages=%s concurrency=%d", run_images, run_messages, concurrency)
    LOGGER.info("================================================================")

    # 1. Ingestion -------------------------------------------------------
    LOGGER.info("[1/%d] Ingestion: loading and validating CSVs ...", TOTAL_STEPS)
    step = time.perf_counter()
    store = load_dataset(dataset_dir)
    # Use the sample requests as the request source when asked, then drop any
    # ground-truth columns so they cannot leak into planning or the LLM.
    source = store.table(requests_table)
    core = source.select(CORE_REQUEST_COLUMNS)
    store.register("requests", core, [], overwrite=True)
    scope_users = core["user_id"].unique().to_list()
    scope_requests = core["request_id"].to_list()
    _apply_scope(store, scope_users, scope_requests)
    LOGGER.info(
        "    scoped to %d user(s) and %d request(s) from '%s'",
        len(scope_users),
        len(scope_requests),
        requests_table,
    )
    for name in store.keys():
        frame = store.table(name)
        LOGGER.info(
            "    %-24s %6d rows x %2d cols", name, frame.height, frame.width
        )
    requests = store.table("requests")
    selected = _request_ids(requests, request_ids)
    selected_users = (
        requests.filter(pl.col("request_id").is_in(selected))["user_id"]
        .unique()
        .to_list()
    )
    is_subset = request_ids is not None
    usage_frames: list[pl.DataFrame] = []
    LOGGER.info(
        "[1/%d] Ingestion done in %.1fs (%d request(s) selected)",
        TOTAL_STEPS,
        time.perf_counter() - step,
        len(selected),
    )

    # 2. Image analysis --------------------------------------------------
    if run_images:
        images = store.table("images")
        if is_subset:
            images = images.filter(pl.col("user_id").is_in(selected_users))
        image_ids = images["image_id"].to_list()
        LOGGER.info(
            "[2/%d] Image analysis: %d image(s) ...", TOTAL_STEPS, len(image_ids)
        )
        step = time.perf_counter()
        image_analyser = ImageAnalyser(
            store=store,
            concurrency=concurrency,
            cache_path=image_cache,
            force=force_images,
        )
        image_result = await image_analyser.run(
            image_ids, progress=progress_logger("images", every=5)
        )
        store = image_result.store
        usage_frames.append(image_result.usage)
        updated = image_result.image_analysis.filter(pl.col("matched")).height
        LOGGER.info(
            "[2/%d] Image analysis done in %.1fs (%d matched, %d cached from %s)",
            TOTAL_STEPS,
            time.perf_counter() - step,
            updated,
            image_result.cache_hits,
            image_result.cache_path,
        )
    else:
        LOGGER.info("[2/%d] Image analysis skipped", TOTAL_STEPS)

    # 3. Cash forecaster: base 90-day ledger per request -----------------
    LOGGER.info(
        "[3/%d] Cash forecaster: building %d base ledger(s) ...",
        TOTAL_STEPS,
        len(selected),
    )
    step = time.perf_counter()
    forecaster = CashForecaster(store=store)
    ledgers = {}
    for index, request_id in enumerate(selected, start=1):
        ledgers[request_id] = forecaster.build_ledger(request_id)
        if index == 1 or index == len(selected) or index % 25 == 0:
            LOGGER.info("    ledgers: %d/%d", index, len(selected))
    total_flows = sum(len(ledger.flows) for ledger in ledgers.values())
    LOGGER.info(
        "[3/%d] Cash forecaster done in %.1fs (%d flows)",
        TOTAL_STEPS,
        time.perf_counter() - step,
        total_flows,
    )

    # 4. Message analysis enriches the ledgers ---------------------------
    if run_messages:
        messages_frame = store.table("messages")
        if is_subset:
            messages_frame = messages_frame.filter(
                pl.col("request_id").is_in(selected)
                | (
                    pl.col("request_id").is_null()
                    & pl.col("user_id").is_in(selected_users)
                )
            )
        message_ids = messages_frame["message_id"].to_list()
        LOGGER.info(
            "[4/%d] Message analysis: %d message(s) ...",
            TOTAL_STEPS,
            len(message_ids),
        )
        step = time.perf_counter()
        message_result = await MessageAnalyser(
            store=store, ledgers=ledgers, concurrency=concurrency
        ).run(message_ids, progress=progress_logger("messages", every=10))
        ledgers = message_result.ledgers
        store.register(
            "message_analysis",
            message_result.message_analysis,
            [],
            overwrite=True,
        )
        usage_frames.append(
            message_result.usage
            if message_result.usage is not None
            else usage_frame([])
        )
        applied = message_result.message_analysis.filter(pl.col("applied")).height
        LOGGER.info(
            "[4/%d] Message analysis done in %.1fs (%d tool call(s) applied)",
            TOTAL_STEPS,
            time.perf_counter() - step,
            applied,
        )
    else:
        LOGGER.info("[4/%d] Message analysis skipped", TOTAL_STEPS)

    # 5. Resolve pending/scheduled events --------------------------------
    LOGGER.info(
        "[5/%d] Engine: resolving pending/scheduled events ...", TOTAL_STEPS
    )
    step = time.perf_counter()
    converter = CurrencyConverter.from_store(store)
    events = store.table("financial_events")
    messages = store.table("messages")
    resolved = 0
    for index, ledger in enumerate(ledgers.values(), start=1):
        resolved += resolve_pending_scheduled_events(
            events, ledger, messages=messages, converter=converter
        )
        if index == 1 or index == len(ledgers) or index % 25 == 0:
            LOGGER.info("    ledgers resolved: %d/%d", index, len(ledgers))
    LOGGER.info(
        "[5/%d] Resolution done in %.1fs (%d event(s) applied)",
        TOTAL_STEPS,
        time.perf_counter() - step,
        resolved,
    )

    # 6. Deterministic payment plans -------------------------------------
    LOGGER.info(
        "[6/%d] Engine: generating payment plans for %d request(s) ...",
        TOTAL_STEPS,
        len(selected),
    )
    step = time.perf_counter()
    engine = PaymentPlanEngine()
    profiles = store.table("financial_profiles")
    options = store.table("request_payment_options")
    request_rows = requests.filter(pl.col("request_id").is_in(selected))
    groups = []
    for index, request in enumerate(request_rows.iter_rows(named=True), start=1):
        profile = profiles.filter(
            pl.col("user_id") == request["user_id"]
        ).row(0, named=True)
        groups.append(
            engine.build(
                request, profile, ledgers[request["request_id"]], options
            )
        )
        if index == 1 or index == len(request_rows) or index % 25 == 0:
            LOGGER.info("    plans: %d/%d", index, request_rows.height)
    plan_options = plan_options_frame(
        [option for group in groups for option in group]
    )
    LOGGER.info(
        "[6/%d] Plans done in %.1fs (%d option(s) generated)",
        TOTAL_STEPS,
        time.perf_counter() - step,
        plan_options.height,
    )

    # 7. Request analyser weighs the plans and writes output.csv ---------
    LOGGER.info(
        "[7/%d] Request analyser: %d LLM decision(s) ...",
        TOTAL_STEPS,
        len(selected),
    )
    step = time.perf_counter()
    analyser_result = await RequestAnalyser(
        plan_options, store=store, concurrency=concurrency
    ).run(selected, progress=progress_logger("requests", every=10))
    analyser_result.output.write_csv(output_path)
    usage_frames.append(analyser_result.usage)
    LOGGER.info(
        "[7/%d] Request analyser done in %.1fs (%d row(s), %d error(s))",
        TOTAL_STEPS,
        time.perf_counter() - step,
        analyser_result.output.height,
        analyser_result.errors.height,
    )

    # Usage and cost report ---------------------------------------------
    usage = (
        pl.concat(usage_frames, how="vertical")
        if usage_frames
        else usage_frame([])
    )
    write_usage_report(
        usage_report_path,
        usage,
        request_count=len(selected),
        dataset_dir=dataset_dir,
        started_at=started_at,
        finished_at=datetime.now(),
    )
    LOGGER.info("Usage report written: %s", usage_report_path)
    LOGGER.info(
        "Pipeline complete: %d row(s) -> %s (%.1fs)",
        analyser_result.output.height,
        output_path,
        (datetime.now() - started_at).total_seconds(),
    )
    if analyser_result.errors.height:
        for error in analyser_result.errors.iter_rows(named=True):
            LOGGER.warning(
                "request %s: %s", error["request_id"], error["error"]
            )

    return {
        "result": analyser_result,
        "plan_options": plan_options,
        "usage": usage,
        "output_path": output_path,
        "usage_report_path": usage_report_path,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Buy or Wait? full pipeline")
    parser.add_argument("--dataset-dir", default=str(DEFAULT_DATASET_DIR))
    parser.add_argument("--output", default=None)
    parser.add_argument("--usage-report", default=None)
    parser.add_argument("--requests", nargs="*", default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="run the 25 sample requests (user_01..user_025) and write sample_output.csv",
    )
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--skip-messages", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "ignore the cached image-analysis results and call the model again "
            "(the cache is also bypassed by deleting its file)"
        ),
    )
    parser.add_argument(
        "--image-cache",
        default=None,
        help="path to the image-analysis JSON cache",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    outcome = asyncio.run(
        run_pipeline(
            dataset_dir=args.dataset_dir,
            output_path=args.output,
            usage_report_path=args.usage_report,
            request_ids=args.requests,
            run_images=not args.skip_images,
            run_messages=not args.skip_messages,
            sample=args.sample,
            concurrency=args.concurrency,
            force_images=args.force,
            image_cache=args.image_cache,
            log_level=getattr(logging, args.log_level),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
