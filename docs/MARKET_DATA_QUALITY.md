# Market-data quality (Stage 2A Phase 3)

Investigation of **stored DexScreener snapshots**. This layer does not call live APIs, does not rewrite `market_snapshots`, and does not change evaluations 1 or 2.

**CLEAN is not a safety rating and not a profit forecast.** Flags are hypotheses about quote quality.

## Checks

| Flag | Meaning |
|---|---|
| `PAIR_CHANGED` | Consecutive `pair_address` values differ |
| `MISSING_LIQUIDITY` | Null liquidity (never treated as zero) |
| `MISSING_VALUATION` | No usable market cap or FDV |
| `STALE_SNAPSHOT` | Last print or consecutive gap older than `QUALITY_STALE_HOURS` (default 6) |
| `DUPLICATE_VALUATION` | Repeated valuation (and price when present) |
| `EXTREME_PRICE_MOVEMENT` | Valuation or `price_usd` ratio **> 10x** in one interval |
| `EXTREME_REVERSAL` | After a >10x rise, a later print is down **≥ 90%** from that high |
| `LIQUIDITY_DISLOCATION` | Both endpoints have liquidity and the ratio exceeds **20x** |
| `POSSIBLE_QUOTE_REGIME_CHANGE` | Extreme move with unchanged pair plus liquidity jump, and/or extreme reversal |
| `INSUFFICIENT_HISTORY` | Fewer than two timestamped snapshots |
| `VALUATION_SOURCE_CHANGED` | Used source switched between market cap and FDV |

Unchanged pair address is **not** proof the print is executable.

## Classifications

Worst flag wins: `INSUFFICIENT_DATA` > `SUSPICIOUS` > `WARNING` > `CLEAN`.

## API

- `POST /api/v1/data-quality/scan` — stored snapshots only; optional `token_address`
- `GET /api/v1/data-quality/status`
- `GET /api/v1/data-quality/summary`
- `GET /api/v1/data-quality/tokens/{token_address}`

Results persist in `data_quality_scans` and `data_quality_findings` (append-only).

## Future historical-evaluation integration (not implemented)

Keep the current evaluator as **all observed outcomes**. A later filter could add parallel summaries:

- All observed outcomes (today’s eval 1 / 2)
- Outcomes passing quality checks (`CLEAN` or `WARNING` without pair change)
- Outcomes excluded from tradeability analysis (`SUSPICIOUS`, pair change, missing liquidity at either end)
- Complete-liquidity subset
- Unchanged-pair subset
- Quote-regime-flagged subset

Raw snapshots and existing evaluation rows stay available for comparison.
