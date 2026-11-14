# Opportunity scoring (Milestone 5, Stage 1)

Paper intelligence only. This engine ranks discovered Solana tokens from **stored** market snapshots, narrative matches, and security assessments. It is **not** a trained predictor and does not claim a token will pump or produce profit.

## Positive opportunity score

Positive components and default weights:

| Component | Weight | Source |
|-----------|-------:|--------|
| Market momentum | 0.25 | Stored DexScreener snapshots |
| Narrative / social momentum | 0.25 | Stored narratives + token matches |
| Early discovery | 0.20 | Pair/discovery timestamps + snapshot count |
| Liquidity quality | 0.10 | Stored `liquidity_usd` and liquidity change |

Weights are configurable (`OPPORTUNITY_WEIGHT_*`). They sum to **0.80**. The remaining **0.20** is a coverage slot for security, not a positive reward.

### Weighted normalization

Only **measured** positive components enter the average. Missing components are omitted, never filled with zero.

```
positive_opportunity_score =
  sum(component_score_i * weight_i) / sum(weight_i of measured components)
```

Each `component_score` is 0–100. Example: momentum 80 (weight 0.25) and liquidity 40 (weight 0.10), narrative and discovery missing:

```
(80 * 0.25 + 40 * 0.10) / 0.35 = 68.6
```

If that token also has completeness 0.35 and confidence 0.41, the **observed** scores stay (68.6) but `status` and `ranking_label` are `INSUFFICIENT_DATA`, never `SCORED` or `HIGH_OPPORTUNITY`.

Narrative is **not** recorded as 0 social activity in that example. It is listed in `missing_components`.

## Security adjustment

Security is separate from the positive score.

```
opportunity_score = clamp(positive_opportunity_score + security_adjustment, 0, 100)
```

- Confirmed penalty only when Milestone 4 classification is reliable: completeness ≥ `SECURITY_MIN_COMPLETENESS` and confidence ≥ `SECURITY_MIN_CONFIDENCE`, and `risk_score` is populated.
- Penalty = `-OPPORTUNITY_MAX_SECURITY_PENALTY * (confirmed_risk_score / 100)` (default max **-20**).
- A high **observed** risk with `INSUFFICIENT_DATA` or `LOW_CONFIDENCE` does **not** apply a confirmed penalty. It is a warning.
- Missing security applies **no bonus**. It reduces completeness and confidence.
- `UNKNOWN` categories are never treated as safe.

## Confidence

```
confidence =
  sum(component_confidence_i * weight_i for measured slots)
  / 1.00
```

Positive weights plus the 0.20 security coverage slot form the denominator. Missing slots contribute 0.

## Data completeness

```
data_completeness =
  (sum of measured positive weights
   + 0.20 if any stored security assessment exists)
  / 1.00
```

Completeness is about **presence of measurements**, not whether a security classification is confirmed.

## Assessment status

Configured floors: `OPPORTUNITY_MIN_COMPLETENESS` (default 0.40), `OPPORTUNITY_MIN_CONFIDENCE` (default 0.50).

| Status | Condition |
|--------|-----------|
| `INSUFFICIENT_DATA` | No positive score, or completeness below floor |
| `LOW_CONFIDENCE` | Completeness meets floor, confidence below floor |
| `SCORED` | Both floors met |

A high `opportunity_score` with `LOW_CONFIDENCE` is **not** a reliable recommendation.

## Ranking labels

| Label | Condition |
|-------|-----------|
| `HIGH_OPPORTUNITY` | `SCORED` and score ≥ `OPPORTUNITY_HIGH_THRESHOLD` (70) and confidence ≥ `OPPORTUNITY_MIN_CONFIDENCE` |
| `WATCHLIST` | Score ≥ `OPPORTUNITY_WATCH_THRESHOLD` (45) and not `HIGH_OPPORTUNITY` (includes some low-confidence rows) |
| `LOW_OPPORTUNITY` | A score exists but is below the watch threshold |
| `INSUFFICIENT_DATA` | Assessment status is `INSUFFICIENT_DATA` or score is null |

## Scan counters

`POST /api/v1/opportunities/scan` reports processed tokens (`tokens_scanned`) separately from classification quality:

- `scored` / `reliable_classifications`: assessments that met both floors
- `low_confidence`: completeness OK, confidence below 0.50
- `insufficient_data`: completeness below 0.40 or no positive score
- `high_opportunity`, `watchlist`, `low_opportunity`: ranking labels
- `tokens_with_single_snapshot`, `tokens_with_narrative_matches`, `tokens_with_confirmed_security`

`insufficient_data: 0` with live `LOW_CONFIDENCE` rows is expected when completeness ≥ 0.40.

## Component notes

- **Momentum:** Price, liquidity, volume, and buy/sell **growth** require two timestamped snapshots with a valid previous value. Acceleration needs three. A single snapshot makes the momentum component `UNAVAILABLE`. DexScreener 24h change fields are not used as a substitute for local history.
- **Narrative:** Opportunity scans reuse stored `token_narrative_matches` (and re-run the DB-only matcher against stored news/narratives). RSS matching only stores a link when a token symbol/name overlaps a narrative keyword, usually with a stored source mention. Unique memecoin tickers rarely appear in general RSS. Reddit/X/Telegram stay `NOT_CONFIGURED` without credentials and do not invent posts.
- **Early discovery:** Age has a sweet spot (about 1–36 hours). Brand-new pairs with thin liquidity are capped. Newer is not automatically better.
- **Liquidity:** Amount and change only. Lock `UNKNOWN` lowers confidence and is not treated as a lock.
- **Security:** Confirmed Milestone 4 classifications may apply a penalty. `UNCONFIRMED` / `UNAVAILABLE` / `UNKNOWN` never mean safe and never add a bonus. `security_coverage` on the assessment records this.

## Freshness

Each token payload includes current `snapshot_count` and `last_snapshot_at`. Each persisted assessment stores the snapshot set it used.

`GET /api/v1/opportunities` does not mix those. If newer stored snapshots exist, the current ranking **recomputes from SQLite** (no DexScreener/RPC call) so score, momentum, confidence, and `assessment.snapshot_count` use the same observations. History endpoints still return the original persisted rows. `freshness` reports assessment time, current vs assessment snapshot counts, age, `is_stale`, and `source` (`persisted` or `recomputed_from_stored_snapshots`). Pass `include_stale=true` only to inspect the unrecomputed persisted row.

## Why this is not a trained ML predictor

There is no outcome labeling, no `signal_outcomes` training set, and no model weights fitted to future price. Component maps are documented heuristics. Later stages may calibrate against historical outcomes; Stage 1 does not.

## Known limitations

- Ranking uses **already stored** rows. It does not call DexScreener or Solana RPC.
- Social momentum is only as good as Milestone 3 matches and adapter configuration.
- Heuristic maps (growth → 0–100) are deterministic but not statistically calibrated.
- Confirmed security can lower a score; it does not yet hard-veto list membership.
- Paper trading is out of scope for this stage.
