# Cash Forecasting & Ledger Tooling

How the deterministic 90-day cash forecast is built (`code/agents/cash_forecaster`)
and how validated tool calls enrich it (`code/tools/forecasting`).

The subsystem is **fully deterministic and offline** — no LLM calls. It answers one
question per request: *what does this user's usable balance look like, day by day,
for 90 days from the request date?* A downstream planner consumes that projection to
decide `amount_safe_to_pay` and the payment method.

---

## 1. Component map

| File | Responsibility |
|---|---|
| `agents/cash_forecaster/config.py` | Tunables: horizon, cadence windows, recurrence thresholds, eligibility filters. |
| `agents/cash_forecaster/models.py` | Pydantic models + typed Polars schemas (`RecurringFlow`, `RecurringEvent`, `ForecastEntry`). Reuses enums from `ingest/models.py`. |
| `agents/cash_forecaster/recurrence.py` | Groups settled events into cadence-classified recurring flows. |
| `agents/cash_forecaster/projection.py` | Calendar arithmetic (`add_months`, `advance`, `retreat`), occurrence generation, day-by-day balance roll-forward. |
| `agents/cash_forecaster/agent.py` | `CashForecaster` — per-request orchestration, currency normalisation, output frames. |
| `agents/cash_forecaster/bridge.py` | Adapts detected flows into a seeded `ForecastLedger`. |
| `tools/forecasting/models.py` | Validated param/state models for the 8 tools + ledger row schema. |
| `tools/forecasting/ledger.py` | `ForecastLedger` — mutable event ledger, 8 tool handlers, enriched projection. |

---

## 2. End-to-end data flow

```
dataset/financial_events.csv ──ingest(polars+pydantic)──► InMemoryDataStore
financial_profiles.csv ─────────────────────────────────────┘
        │
        ▼  CashForecaster.run(requests)
   ┌─────────────────────────────────────────────────────────────┐
   │ per request:                                                │
   │  1. profile(user) → home_currency, current_available_balance│
   │  2. filter settled cash events for user with event_date≤req │
   │  3. normalise foreign amounts → home currency (dated rate)  │
   │  4. detect_recurring() → RecurringFlow[] + RecurringEvent[]  │
   │  5. stamp first_forecast_date; build_forecast() → 91 rows   │
   └─────────────────────────────────────────────────────────────┘
        │                                    │
        ▼                                    ▼
  forecast dataframe                 recurring_flows / recurring_events
  (request_id × 91 days)             (audit trail of what was detected)
        │
        ▼  CashForecaster.build_ledger(request_id)
  ForecastLedger(opening_balance, start=request_date, recurring occurrences)
        │
        ▼  apply(tool, params)…            ── 8 validated tools
  ForecastLedger.to_frame()
  (daily closing / hold / available balance)
```

`CashForecastResult` bundles `forecast`, `recurring_flows`, `recurring_events`, and
the source `store`.

---

## 3. Recurrence detection (`recurrence.py`)

**Eligibility filter** (`agent.detect_recurring_for_user`, `config.py`):

- same `user_id`, `event_date <= request_date`
- `status == settled` only (`DETECTION_STATUSES`)
- `direction ∈ {credit, debit}` (`CASH_DIRECTIONS`)
- `flexibility ∈ {fixed, reducible, stoppable, reducible_or_stoppable}`
- non-null amount

Events are grouped by `user_id` plus the four `GROUP_KEYS`
(`event_type, category, description, direction`) and a group becomes a
`RecurringFlow` only if **all** hold:

1. `occurrences >= MIN_RECURRENCES (3)`.
2. At least 2 positive date gaps.
3. `median(gaps)` falls in a cadence window: weekly 6–8, biweekly 13–15, monthly 26–32.
4. The fraction of gaps inside that window is `>= MIN_CADENCE_CONSISTENCY (0.6)`.

**Flow fields:** `cadence`, `period_days`, `amount = median(member amounts)`,
`first_date`, `last_date`, and for monthly flows `anchor_day` = the **modal
day-of-month** (tie-broken toward the earliest). The modal anchor avoids a single
stray month-end payment dragging every future projection to the 31st.

`RecurringEvent` records each contributing row (`event_id`, amount, `event_date`,
cadence) for auditability.

---

## 4. Projection (`projection.py`)

**Occurrences** in `[start, end]`:

- Weekly/biweekly: step `±period_days` from `last_date` in both directions.
- Monthly: shift `last_date` by whole months (month-end clamped) and force the day to
  `anchor_day` (clamped per month), so the 31st does not drift.

**Balance roll-forward** (`build_forecast`):

```
day 0  → closing_balance = current_available_balance
day n  → closing_balance = prev + inflows_n − outflows_n
```

Occurrences on **day 0 are intentionally skipped**: the opening balance is the
already-current balance, so a settled flow due on the request date is already baked
in. This is the same convention as `ForecastLedger.forecast()`, which only schedules
flows with `start < flow.date <= end`.

The agent emits 91 rows per request (day 0…90). Verified on the full dataset:
250 requests → 22 750 rows, day-0 balance equals `current_available_balance` for all.

---

## 5. Currency normalisation

`CashForecaster._home_amount` converts a foreign event using the **settlement date**
(falling back to `event_date`) and `CurrencyConverter` (direct, inverse, or shortest
cross-currency path from the fixed `exchange_rates` table). Conversion failures are
**not** silently ignored — an amount is never mixed into the home-currency total
unconverted.

`CurrencyConverter._adjacency` builds a deterministic rate graph: **direct quoted
rates always win over an inverse-derived rate** for the same pair (both
`USD→EUR = 0.92` and `EUR→USD = 1.09` exist and are mutually inconsistent, so the
inverse `1/1.09` must not overwrite the direct `0.92`). Without this precedence the
same conversion could return two different values across calls.

`RecurringFlow.currency` / `RecurringEvent.currency` are always the home currency.
The frame builders dump enums to plain strings, so frame columns stay `String`.

---

## 6. The ledger and its tools (`tools/forecasting`)

`ForecastLedger` holds concrete `ScheduledFlow` occurrences plus non-cash
`LiquidityHold`s and `InformationalNotice`s. Every tool call is a Pydantic-validated
params model (`TOOL_PARAMS`), dispatched via `apply(tool, params)` / `apply_many`.

| # | Tool | Effect |
|---|---|---|
| 1 | `update_recurring_stream` | From `effective_date`, set fixed amount, scale by multiplier, or terminate exactly one `stream_id`. |
| 2 | `apply_one_off_adjustment` | Override one instance's amount, or add a standalone lump-sum credit/debit. |
| 3 | `reschedule_transaction_date` | Move occurrence(s) to a new settlement date (by `related_event_id` or category+date). |
| 4 | `schedule_receivable_or_payable` | Insert a confirmed one-off or recurring future flow. |
| 5 | `resolve_pending_transaction` | Confirm/update/dispute/cancel a pending transaction. |
| 6 | `flag_internal_transfer` | Mark matching legs internal so they no longer affect cash. |
| 7 | `apply_liquidity_hold` | Reserve spendable balance until `release_date`. |
| 8 | `mark_informational_or_unconfirmed` | Log a non-cash/unconfirmed notice with zero forecast impact. |

**Cash-activation rules** (`_is_cash_active`): `cancelled`, `internal`, and
`disputed` flows are excluded; **pending credits are excluded**, but pending debits
*do* reserve cash — matching the problem rules. Holds subtract from
`available_balance` (not `closing_balance`) across their active window; forecasts
therefore expose both raw and spendable balances.

---

## 7. Shared schema discipline (`ingest/models.py`)

The forecaster and ledger do not invent their own currency/event vocabulary. They
import and enforce the ingest enums:

- `cash_forecaster.models`: `RecurringFlow`/`RecurringEvent` use `EventType`,
  `EventCategory`, `Direction`, `Currency`, `Flexibility`.
- `tools.forecasting.models`: `currency` fields use `Currency`; `FlowDirection`
  and `FlowStatus` are defined **from** `Direction` and `EventStatus` values so
  the shared values cannot drift.

`Cadence` is the only new enum, since it is a forecasting concept absent from ingest.

---

## 8. Verified invariants

- 250 requests → 22 750 forecast rows (91 each); no nulls.
- Day-0 closing balance `== current_available_balance` for every request.
- 0 currency-conversion failures across 140 foreign-currency events; repeated
  conversions are now bit-stable (500 calls, one value).
- Seeded-ledger baseline projection matches the agent's `forecast` for **all** 250
  requests (0 mismatched rows) now that conversions are deterministic.
- All 8 tools validate, apply, and mutate the enriched frame; a hold stops affecting
  `available_balance` after its release date.
- Modal monthly anchors fix the 38 flows whose max day-of-month differed from the
  typical day.

---

## 9. Gap register (known limitations)

Ordered by impact.

1. **Pending/scheduled one-offs are not seeded.** Detection uses settled events only,
   so 63 pending debits, 47 scheduled credits (the "next confirmed salary"), 16
   scheduled expenses, and 7 scheduled debt payments are invisible unless a settled
   history generates a projection. The ledger has no loader for these rows either, so
   tools 3/5/6 (which target `event_id`) currently have no seeded pending events to
   act on. An orchestrator must call `schedule_receivable_or_payable` /
   `add_flow(..., event_id=…)` to introduce them.
2. **Cadence detection keys on `event_date`, not `settlement_date`.** 14 income rows
   differ between the two, so projected income can precede the actual cash date.
3. **No currency conversion inside the ledger.** A `schedule_receivable_or_payable`
   call whose `currency` differs from the ledger currency is added at face value.
4. **Stream ids are synthetic and must come from the ledger.** Tool 1 now targets an
   exact `stream_id` (`stream::user|event_type|category|description|direction`), so a
   caller must read the candidate ids from the ledger/forecast rather than guess a
   category. The message analyser surfaces them as `CANDIDATE_STREAMS`.
5. **Recurrence is heuristic.** Three observations with 60 % in-window gaps can be
   accepted even with an anomalous gap; constant-median amounts understate rising
   essentials; `linked_event_id` lifecycles (debt/investment) are not de-duplicated.
6. **Ambiguity is silent in some tools.** `apply_one_off_adjustment` override takes
   the first matching instance; `schedule_receivable_or_payable` accepts past and
   beyond-horizon dates that `forecast()` then drops; `apply_many` mutates partially
   on a mid-sequence error.
7. **`forecast()` drops debits dated on day 0** (`start < flow.date`). Correct for
   settled items, potentially wrong for a pending debit dated the request date.

---

## 10. Running and verifying

```powershell
# ingest + full forecast
.\.venv\Scripts\python.exe -m code.ingest
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from code.agents.cash_forecaster import CashForecaster; r=CashForecaster().run(); print(r.forecast.height, r.recurring_flows.height, r.recurring_events.height)"
```

The subsystem needs no API keys and writes no files; it returns typed Polars frames.
