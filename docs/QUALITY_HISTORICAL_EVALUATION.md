# Quality-aware historical evaluation (Stage 2A Phase 5)

Separate from evaluations 1 and 2. Stored DexScreener snapshots only. Not executable returns.

## Look-ahead safeguard

Each measured outcome is scored from:

- the signal snapshot
- the path through the outcome snapshot
- at most the snapshot immediately before the signal

Snapshots after `outcome_at` are ignored. Token-level rows in `data_quality_findings` are not applied retroactively.

An extreme reversal is flagged only when that reversal is already visible at the outcome timestamp.

## Cohorts

An outcome can belong to several diagnostic cohorts. Percentages use that cohort’s `sample_size` as the denominator.

| Cohort | Definition |
|---|---|
| `ALL_OBSERVED` | All measured paths (same pairing as the original evaluator) |
| `SAME_PAIR` | Same `pair_address` at both ends |
| `SAME_PAIR_AND_VALUATION` | Same pair and same valuation source |
| `COMPLETE_LIQUIDITY` | Non-null liquidity at both ends |
| `QUALITY_FILTERED` | No time-specific quality-filter flags (`CLEAN`) |
| `SUSPICIOUS_OR_EXCLUDED` | Measured outcomes not in `QUALITY_FILTERED`, with reasons |

`QUALITY_FILTERED` is not a safety or profit rating.

## Overlapping one-hour windows

Raw overlapping outcomes stay in `ALL_OBSERVED`.

Dedup: per token, sort by signal time; keep a window if `signal_at >=` previous kept `outcome_at`. Diagnostic cohorts `ALL_OBSERVED_NON_OVERLAPPING` and `QUALITY_FILTERED_NON_OVERLAPPING` use that subset.

## Net-positive

`net-positive` means **gross return > round-trip cost** (default 0.016). These are hypothetical DexScreener valuation changes, not fills.

## API

- `POST /api/v1/historical/quality-evaluate`
- `GET /api/v1/historical/quality-evaluations`
- `GET /api/v1/historical/quality-evaluations/{id}`
- `GET /api/v1/historical/quality-status`

Original `/api/v1/historical/evaluate` routes are unchanged.
