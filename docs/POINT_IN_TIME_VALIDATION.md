# Point-in-time opportunity validation (Stage 2A Phase 6)

Not paper trading. Not fills. Not Stage 2B.

## Candidate generation

Every stored `market_snapshots` row is a candidate. Signal time **T** = `recorded_at`.

Candidates are created **before** any future pairing. A last print with no later observation remains G0.

## Duplicate timestamps

Snapshots are ordered by `(recorded_at, id)`.

Features for a candidate = **that snapshot** plus prints with `recorded_at` **strictly before** T. Same-timestamp peers are excluded.

Outcomes must have `recorded_at` **strictly after** T. Same-timestamp rows are never outcomes.

## Entry groups (before overlap)

- **G0:** all candidates.
- **G1:** E1–E6 (history ≥2 with a strictly earlier print; valuation; price; pair matches previous; no already-visible reversal/quote-regime; opportunity reconstructed as-of T). **Null liquidity allowed.**
- **G2:** G1 with non-null `liquidity_usd >= 10000` at T (experimental cut).
- **G3:** G2 and as-of-T `SCORED` + `HIGH_OPPORTUNITY` using **frozen** weights (70 / 0.50 / 0.40).

`opportunity_assessments` rows are never used as historical scores. Phase 5 `QUALITY_FILTERED` is not an entry group.

## Outcome coverage

After entry labels, pairing runs independently for `next_observation` and `one_hour` (30-minute max gap). Statuses: `measured`, `missing_outcome`, `insufficient_history`, `gap`, `missing_outcome_valuation`.

Missing outcomes stay in G0–G3 counts. They are **not** zero returns. Performance denominators are **measured** counts.

## Liquidity

Never impute null as zero. Report G1 missing-liquidity % and G2/G1. Outcome liquidity coverage is separate. `$10,000` is experimental, not executable.

## Overlap

`baseline`, `1h`, `6h`. Applied **inside each group**. An ineligible print cannot block an eligible print in another group. Compare groups only under the **same** policy.

## Research / holdout / purge

`T0` = min snapshot time. `H` = T0 + 48h.

- Research: `T0 <= T < H - 90 minutes`
- Purge: `H - 90 minutes <= T < H`
- Holdout: `T >= H`

90 minutes = 1-hour horizon + 30-minute gap. Chronological test, not independent tokens. Holdout measured n &lt; 30 → insufficient data. Do not retune.

## Current data

About three days of 15-minute history, ~49% missing liquidity, little security/narrative as-of T. **G3 may be zero. Holdout may be insufficient.** Thresholds are not relaxed.

## API

`POST /api/v1/historical/point-in-time-evaluate`  
`GET /api/v1/historical/point-in-time-status`  
`GET /api/v1/historical/point-in-time-runs`  
`GET /api/v1/historical/point-in-time-runs/{id}`
