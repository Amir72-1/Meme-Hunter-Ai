# Data readiness (Stage 2A Phase 7)

Read-only monitoring of stored DexScreener history. Not paper trading. Not Stage 2B.

## Endpoints

- `GET /api/v1/readiness`
- `GET /api/v1/readiness/health`
- `GET /api/v1/readiness/continuity`

No new tables. Existing evaluations, quality runs, point-in-time rows, and snapshots are not rewritten.

## What it reports

Coverage (tokens, snapshots, span), observation-count and history-span histograms, snapshot-interval distribution, liquidity and valuation missingness (overall and by UTC date), stale last prints, pair changes, security/narrative coverage, latest opportunity labels, and G3 exclusion reasons from the **latest point-in-time run**.

Null liquidity is never treated as zero. Every rate has a denominator.

## Collection health

Healthy only if the in-process scheduler is **enabled and running**, there is a **completed** `discovery_scans` row, **zero consecutive failures**, and the latest snapshot is no older than **twice** the expected interval. Enabled alone is not healthy. Scheduler counters reset on restart; SQLite history does not.

## Repeat Phase 6 when

About 14 days of continuous 15-minute collection, enough tokens with ≥7 days of history, a holdout longer than the 90-minute purge, and G3 (SCORED + HIGH_OPPORTUNITY as-of T) able to reach n≥30 **without lowering floors**.
