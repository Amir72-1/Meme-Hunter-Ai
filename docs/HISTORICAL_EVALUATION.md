# Stage 2A Phase 1 — Market-only historical evaluation

Paper intelligence only. This module **does not** trade, connect a wallet, or call DexScreener.

## What it measures

For each stored token, every snapshot is a candidate **signal time** `T`. Features at `T` use only snapshots with `recorded_at <= T`. Later snapshots are used **only** as outcomes.

Supported horizons:

- `next_observation` — the next snapshot after `T`, if the gap is **greater than 0 and at most 30 minutes**.
- `one_hour` — the first snapshot at least 60 minutes after `T`, reached along a **continuous** path where every consecutive gap is ≤ 30 minutes.

5-minute and 24-hour horizons are not implemented.

## Valuation and missing data

- Percentage change uses **market cap if present and > 0**, otherwise **FDV**.
- Missing liquidity is **never** replaced with zero. Liquidity change is omitted when either side is missing.
- Volume change is omitted when either side is missing or non-positive.
- Gaps **> 30 minutes** are excluded as missing intervals, not treated as a 15-minute bar.

## Costs

Configurable `EVALUATION_FEE_BPS` and `EVALUATION_SLIPPAGE_BPS` (defaults 30 and 50).

Estimated net return = gross return − 2 × (fee + slippage) / 10,000.

That subtracts a **hypothetical round-trip** (entry and exit). It is **not** a fill, quote, or execution price.

## Categories

If the previous snapshot is within 30 minutes and both valuations exist:

- `PRIOR_UP` / `PRIOR_DOWN` / `PRIOR_UNCHANGED`

Otherwise `NO_PRIOR`. Opportunity scores, narratives, and security labels are **not** used in Phase 1 (they are incomplete and would mix other pipelines).

## Metrics

Sample size, win rate (share of measured valuation returns > 0), average/median gross and net return, max favorable and adverse movement along the path, coverage and exclusion counts.

If sample size < `EVALUATION_MIN_SAMPLE_FOR_CLAIMS` (default 30), `claim_ready` is false. Even above that threshold, results describe **this archive only** and do not guarantee future profitability.

## API

- `POST /api/v1/historical/evaluate` — run on SQLite snapshots; body: `horizon`, optional `fee_bps`, `slippage_bps`, `token_address`
- `GET /api/v1/historical/evaluations` — saved summaries
- `GET /api/v1/historical/evaluations/{id}` — one summary
- `GET /api/v1/historical/status`

Summaries are stored in `historical_evaluations`. Market snapshots are never updated or deleted.

## Limitations

- ~15-minute sampling, not candles.
- Feed rotation creates gaps.
- Liquidity is often missing.
- No execution, slippage is a constant, not order-book based.
- Survivorship and listing bias in DexScreener feeds.
- Past paths ≠ future returns.
