# Development schema updates

SQLite is migrated additively on startup via `migrate_schema()` in `app/database.py`.

- New tables are created with `create_all`.
- Missing columns are added with `ALTER TABLE ... ADD COLUMN`.
- Existing rows are kept. Columns are never dropped.

## Milestone 2 additions

### `tokens` (added columns)

- `dex_name`
- `pair_address`
- `pair_created_at`
- `discovery_source`
- `last_discovered_at`
- `filter_status` (`meets_threshold` or `below_threshold`)
- `filter_reasons`

### `market_snapshots` (added columns)

- `volume_5m`, `volume_1h`
- `fdv`
- `buys_5m`, `sells_5m`, `buys_1h`, `sells_1h`, `buys_24h`, `sells_24h`
- `dex_name`, `pair_address`, `pair_created_at`
- `active_boosts`
- `discovery_source`
- `filter_status`, `filter_reasons`

Snapshots are append-only. A scan never overwrites prior `market_snapshots` rows.

Each discovery scan also refreshes previously stored tokens (up to `DISCOVERY_REFRESH_STORED_LIMIT`) so feed rotation does not leave old tokens with a single observation.

Automatic collection: FastAPI lifespan runs one in-process asyncio task when `AUTO_DISCOVERY_ENABLED=true` (default interval `AUTO_DISCOVERY_INTERVAL_MINUTES=15`). The first automatic scan waits one interval after startup. See `docs/AUTO_DISCOVERY.md`. Scheduler run counters are **in-memory** and reset on restart; snapshot history is not rewritten.

`market_snapshots.recorded_at` is stored as UTC. Naive SQLite values from older rows are interpreted as UTC on load. Existing rows are not rewritten.

### `discovery_scans` (new table)

Stores each scan’s counts, errors, skipped records, and timestamps.

Missing DexScreener fields are stored as SQL `NULL`. Values are never invented.

## Milestone 3 additions

Additive columns on `news_items`: `category`, `normalized_title`, `feed_url`.

Additive columns on `narratives`: keywords, mention/engagement metrics, spam/confidence/completeness, explanation.

New tables (create_all, no drops):

- `social_posts`
- `narrative_mentions`
- `token_narrative_matches`
- `narrative_score_history`
- `intelligence_scans`

## Milestone 4 additions

New tables (create_all only, no drops):

- `token_security_scans`
- `holder_analyses`
- `liquidity_analyses`
- `wallet_activities`
- `token_risk_assessments`
- `risk_score_history`

Historical assessments are append-only. Missing measurements stay SQL `NULL` / `UNKNOWN`.

Additive columns on `token_risk_assessments` and `risk_score_history`:

- `observed_risk`
- `observed_risk_band`

`risk_score` is the final classification and is null unless status is `scored`.

## Milestone 5 Stage 1 additions

New append-only table `opportunity_assessments` (`create_all` only, no drops):

- `token_id`, `assessed_at`
- `status` (`SCORED`, `LOW_CONFIDENCE`, `INSUFFICIENT_DATA`)
- `ranking_label` (`HIGH_OPPORTUNITY`, `WATCHLIST`, `LOW_OPPORTUNITY`, `INSUFFICIENT_DATA`)
- `opportunity_score`, `positive_opportunity_score`, `security_adjustment`
- `confidence`, `data_completeness`
- `available_components_json`, `missing_components_json`, `component_scores_json`
- `positive_signals_json`, `negative_signals_json`, `warnings_json`
- `explanation`

Scans append a new row; prior assessments remain. Opportunity ranking does not rewrite market, narrative, or security history.

## Stage 2A Phase 8 — interruption recovery

No new tables. `discovery_scans.status` may be `interrupted` for leftover `running` rows after a crash. Existing completed/failed rows are not rewritten. `GET /api/v1/scheduler/status` and `GET /api/v1/readiness/continuity` are read-only.

## Stage 2A Phase 7 — data readiness

No new tables. `GET /api/v1/readiness` reads existing snapshots, discovery scans, security, narratives, opportunity assessments, and the latest point-in-time run. It does not rewrite historical rows.

## Stage 2A Phase 6 — point-in-time validation

New tables (`create_all` only): `point_in_time_runs`, `point_in_time_candidates`, `point_in_time_outcomes`. They do not rewrite snapshots, `historical_evaluations`, `quality_historical_*`, or `opportunity_assessments`.

## Stage 2A Phase 5 — quality-aware historical outcomes

New tables (`create_all` only): `quality_historical_evaluations`, `quality_historical_outcomes`. They do not rewrite `historical_evaluations`, `data_quality_scans`, or `market_snapshots`.

## Stage 2A Phase 3 — data quality

New tables `data_quality_scans` and `data_quality_findings` (`create_all` only). They do not rewrite `market_snapshots` or `historical_evaluations`.

## Stage 2A Phase 1 — historical evaluations

New table `historical_evaluations` (`create_all` only, no drops). Stores evaluation **summaries**. Does not rewrite `market_snapshots`.

- `horizon` (`next_observation`, `one_hour`)
- hypothetical `fee_bps`, `slippage_bps`
- coverage counters and `summary_json`

## Automatic discovery scheduler

No new SQLite tables. Run counters live in process memory (`GET /api/v1/scheduler/status`) and reset on restart. Completed scans still append `discovery_scans` and `market_snapshots`.

Additive columns on `opportunity_assessments`:

- `snapshot_count`
- `last_snapshot_at`
- `snapshot_age_hours`
- `narrative_match_count`
- `security_coverage` (`CONFIRMED`, `UNCONFIRMED`, `UNAVAILABLE`)

## Stage 2A Phase 9B — Metaplex provenance

Additive columns only (no drops, no rewrites):

- `token_security_scans`: `source`, `observed_at`, `recorded_at`, `slot`, `metadata_status`, `metadata_account`, `metadata_update_authority`, `metadata_name`, `metadata_symbol`, `metadata_uri`, `metadata_is_mutable`
- `token_risk_assessments`: `source`, `observed_at`, `recorded_at`, `slot`
- `news_items`: `observed_at`, `recorded_at`

Security scans still append new rows. RSS `published_at` stays NULL when the feed has no usable publication time. Point-in-time reconstruction uses `observed_at` (else `scanned_at`) at or before T.

## Stage 2A Phase 9C.1 — Token-2022 extension observations

New append-only table `token_2022_extension_observations` (no drops, no rewrites of historical security scans):

- mint address, token program, inspection status (`AVAILABLE`, `NO_EXTENSIONS`, `UNAVAILABLE`, `UNSUPPORTED`)
- raw extension type names and safely parsed public fields
- transfer-fee, transfer-hook, permanent delegate, default account state, pausable, mint-close, confidential-transfer, metadata pointer / TokenMetadata, and ScaledUiAmount indicators when present
- `parser_complete`, `parser_notes`, `source`, `observed_at`, `recorded_at`, `slot`, findings/errors

`observed_at` is the successful RPC parse time (null on RPC failure). `recorded_at` is persistence time. PIT queries must use `observed_at <= T` and must not fill earlier gaps with later rows. Extension inspection is evidence only and does not change opportunity scoring.

## Stage 2A Phase 9C.2 — Explainable control-risk findings

No new tables. Findings are derived at query time from `token_2022_extension_observations` and `token_security_scans` using `observed_at <= signal_time`. They are evidence only and do not rewrite historical rows or change opportunity scoring.

## Stage 2A Phase 9C.3 — Null-aware market condition monitor

No new snapshot columns. The monitor reads `market_snapshots` only (`recorded_at <= T`) and may append `intelligence_scans` rows with `scan_type=market_monitor`. Missing liquidity is never stored or interpreted as zero. Opportunity scores, historical evaluations, and PIT runs are not modified.

Phase 9C.3.2: collector health uses a bounded discovery-scan history; findings separate `market_data_provider` (`dexscreener`) from snapshot `discovery_source`; scan metadata reports `requested_limit`, `applied_limit`, and `max_scan_limit`.

## Stage 2A Phase 9C.4B — Creator mint-initialization observations

New append-only table `creator_observations` (`create_all` only; no drops; historical rows are never rewritten):

- `token_id`, `mint_address`, `token_program`, `inspection_status`
- `mint_init_signature`, `mint_init_slot`, `mint_init_block_time`, `mint_init_signer`, `mint_init_status`
- `fee_payer`, `signer_addresses_json`, `create_program_id`, `create_instruction`
- `source`, `observed_at`, `recorded_at`, `slot`, `parser_notes`, `errors_json`

Statuses are `VERIFIED`, `UNKNOWN`, or `UNAVAILABLE`. Empty or missing RPC history is never `VERIFIED_ABSENT`. `observed_at` is set after a candidate transaction is successfully parsed (and preserved on RPC cache hits). `recorded_at` is insert time. Point-in-time queries use `observed_at <= T` only. `wallet_activities.deployer_address` is not modified. Opportunity scoring is unchanged.

## Stage 2A Phase 9C.4C — Token-account owner observations

New append-only table `token_account_owner_observations` (`create_all` only; no drops; historical rows are never rewritten):

- `token_id`, `mint_address`, `token_account_address`, `token_account_owner`, `token_account_amount`, `token_account_status`
- `owner_classification` (`WALLET`, `BURN_OR_INCINERATOR`, `AMM_OR_LIQUIDITY_VAULT`, `PROTOCOL_ACCOUNT`, `UNKNOWN`)
- `classification_status` (`VERIFIED`, `UNKNOWN`, `UNAVAILABLE`)
- `classification_evidence`, `token_program`, `source`, `observed_at`, `recorded_at`, `slot`, `parser_notes`, `errors_json`

Token-account address, owner, and amount are stored separately. Top largest accounts are not a complete holder census. Missing account bodies are not verified absence. Point-in-time queries use `observed_at <= T` and exclude null `observed_at`. Opportunity scoring, security floors, and `creator_observations` are not rewritten.

## Stage 2A Phase 9C.4D — Metaplex creator-vector observations

New append-only table `metaplex_creator_observations` (`create_all` only; no drops; historical rows are never rewritten):

- `token_id`, `mint_address`, `metadata_account`, `metadata_status`
- `creator_vector_status` (`VERIFIED`, `VERIFIED_ABSENT`, `UNKNOWN`, `UNAVAILABLE`)
- `creators_json` (address, verified, share, order), `creator_count`, `verified_creator_count`
- `source`, `observed_at`, `recorded_at`, `slot`, `parser_notes`, `errors_json`

`VERIFIED_ABSENT` is only for a complete metadata account with Option::None or a valid empty `creators[]`. Null RPC accounts are `UNAVAILABLE`. `observed_at <= T` only; null `observed_at` is excluded. Metaplex creators are not mint-init signers, fee payers, mint authority, update authority, or token-account owners. Opportunity scoring is unchanged.

## Stage 2A Phase 9C.5 — Unified creator evidence panel

No new tables. `GET /api/v1/creator/evidence/{mint}` is a read-only query-time aggregation of `creator_observations`, `token_account_owner_observations`, and `metaplex_creator_observations`. Optional `as_of` uses each source's `observed_at <= T` independently. Exact address strings shared across sources are labeled `ADDRESS_MATCH_ACROSS_SOURCES` only. No RPC, no writes, no identity merge, no scoring changes.

## Stage 2A Phase 9C.6.1 — RPC accounting and attempt timestamps

Additive nullable `attempted_at` on `creator_observations`, `token_account_owner_observations`, and `metaplex_creator_observations`. Existing rows are not rewritten.

- `observed_at`: successful evidence retrieval and parse (unchanged). Point-in-time queries still use `observed_at <= T` only and exclude null `observed_at`.
- `recorded_at`: insert time (unchanged).
- `attempted_at`: RPC/scan attempt time, including failures. Failed requests do not receive a fabricated `observed_at`.

Mint signature pagination continues until a short page or `CREATOR_SIGNATURE_PAGE_LIMIT` (default 20). Hitting the page cap is truncated history (`UNKNOWN` / `UNAVAILABLE`), never `VERIFIED_ABSENT`. Scan summaries record actual HTTP attempts, retries, 429s, timeouts, and stop the current batch after repeated 429s without scanning further tokens. Opportunity scoring and historical evaluation logic are unchanged.

## Stage 2A Phase 9C.6.3 — Shared RPC session and cooldown

Additive table `rpc_cooldown_states` (singleton row per `scope`, default `solana_rpc`). This is operational control state, not evidence. Existing observation tables are unchanged.

- `POST /api/v1/creator/scan`, `/holders/scan`, `/metadata/scan`, and `/batch` share one process-wide `SolanaRpcClient` for an evidence batch. HTTP resources close on application shutdown. Concurrent scans cannot create a second shared client.
- Repeated HTTP 429s that set `RATE_LIMITED` persist a cooldown: `until_at` is `Retry-After` when provided, otherwise `SOLANA_RPC_COOLDOWN_BASE_SECONDS * 2^(consecutive_429-1)`, capped at `SOLANA_RPC_COOLDOWN_MAX_SECONDS` (defaults 5s / 60s). Cooldown is finite; it does not retry indefinitely.
- While `until_at` is in the future, every `SolanaRpcClient` (including a newly constructed one) refuses HTTP and surfaces `COOLDOWN_ACTIVE`. Individual scan endpoints return HTTP 429. Conflicting in-progress scans return HTTP 409. No observation is written for a cooldown-blocked request.
- `POST /api/v1/creator/batch` runs mint-init, holders, then Metaplex on the shared client. A rate-limit stop skips remaining operations and does not start the next token. Batch `status` is not `completed` if any operation was skipped.
- `rpc_calls` remains actual HTTP attempts. Point-in-time queries still use successful `observed_at` only.

## Stage 2B Phase 10A — Paper trading foundation

Additive tables (no historical observation rewrites):

- `paper_portfolios` — simulated PAPER_ONLY USD book (`starting_cash_usd`, `cash_available_usd`, `cash_reserved_usd`, realized PnL, fees, slippage as decimal text)
- `paper_orders` — simulated orders with `client_order_id` uniqueness per portfolio
- `paper_positions` — quantity, average cost, cost basis
- `paper_trades` — fills with observed vs simulated prices
- `paper_equity_snapshots` — append-only valuation; `equity_usd` null when marks are UNKNOWN
- `paper_decisions` — reasons for accepts/rejects

Default portfolio is initialized idempotently at $100 USD. No wallet. No chain. Manual `POST /api/v1/paper/simulate` only; no automatic orders. Execution uses `market_snapshots.price_usd` with recorded timestamp; market cap/FDV are not prices. Missing liquidity is not zero. Quote age default 60s.

## Stage 2B Phase 10B.1 — Fresh market quote service

Additive append-only table `paper_market_quotes`, distinct from scheduled `market_snapshots`. On-demand DexScreener fetches for an explicitly requested catalog mint only (no universe polling). Columns include `price_usd`, `liquidity_usd` (null when missing, never coerced to zero), `pair_address`, `source`, `retrieved_at` (HTTP time), `market_observed_at` (provider timestamp when present), `freshness_basis` (`PROVIDER_TIMESTAMP` or `RETRIEVAL_UNPROVEN`), `status` (`ACCEPTED` / `REJECTED` / `PROVIDER_UNAVAILABLE`), pair-match and discontinuity flags, and `cache_hit`. Historical snapshots and evaluation tables are not rewritten.

`paper_orders.paper_quote_id` is an additive nullable pointer. Paper fills prefer an ACCEPTED on-demand quote with a provider timestamp within `PAPER_MAX_QUOTE_AGE_SECONDS` (default 60). Retrieval time alone is not executable freshness. Tracked `tokens.pair_address` is used when present; the service does not silently switch pairs.

## Stage 2B Phase 10B.2 — Opportunity decision engine

Additive table `paper_opportunity_decisions` linked to existing `paper_decisions`. Append-only BUY_CANDIDATE / WAIT / REJECT records from stored opportunity, security, market-quality, quote, and portfolio evidence. No DexScreener or Solana RPC during evaluation. No automatic orders. Confirmed hard REJECT (high/critical security, invalid pair/quote, hard portfolio limits) overrides WAIT from missing evidence. Floors are unchanged: opportunity completeness 0.40 / confidence 0.50 / HIGH_OPPORTUNITY 70; security completeness/confidence 0.50; quote age 60s; liquidity $10,000.

## Stage 2B Phase 10C.1 — Automatic paper entry

Additive tables `paper_auto_entry_cycles`, `paper_auto_entry_attempts`, and singleton `paper_auto_entry_locks`. Cycles record MANUAL or SCHEDULED simulated buys only. `PAPER_AUTO_ENTRY_ENABLED` defaults false. The scheduler does not catch up after restart. Evaluation uses stored evidence; DexScreener quote refresh is not invoked by the entry engine. Simulated fills are never labeled as market executions. No automatic selling.

## Stage 2B Phase 10C.2 — Automatic paper exits

Additive tables `paper_auto_exit_cycles`, `paper_auto_exit_attempts`, and append-only `paper_position_observations`. `paper_auto_entry_locks.cycle_kind` is additive so entry and exit share one SQLite lease. `PAPER_AUTO_EXIT_ENABLED` defaults false. The scheduler waits one full interval after start (no catch-up sells). Trailing highs use only accepted observations with `opened_at <= observed_at <= now`; unobserved peaks are never inferred. Security exits require a newly observed reliable high/critical classification after entry; missing or unreliable security is not deterioration. Stop 10% / take-profit 20% / trailing 10% after 15% / max hold 24h are configurable defaults, not claimed optimal. Max-hold and other triggers without a valid quote record `EXIT_PENDING_DATA`; an unfilled stop is not successful protection. No DexScreener refresh. Simulated sells are never market executions.

