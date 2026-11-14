# MEME HUNTER AI — Development roadmap

Personal product first. SaaS is Stage 10. Paper trading only.

## Stage status

| Stage | Name | Status |
|-------|------|--------|
| 0 | Repo / venv / requirements | **Done** |
| 1 | Backend setup and database | **Done** |
| 2 | DexScreener market-data integration | **Done** (automatic 15-minute collection while backend runs) |
| 3 | News and RSS intelligence | **Done** |
| 4 | Narrative extraction and classification | Not started |
| 5 | Social integrations | Not started |
| 6 | Token momentum scoring | **Stage 1 done** (explainable opportunity ranking; not ML) |
| 7 | Security and wallet intelligence | **Done** |
| 8 | Historical data and model training | **Stage 2A Phase 1–8** (collection resilience; no trading) |
| 9 | Paper trading and backtesting | Not started |
| 10 | SaaS architecture | Not started |

## Stage 1 — Backend setup and database

**Goal:** a runnable FastAPI app with SQLite, settings, and empty-but-real health/config surfaces.

Deliverables:

- `app/main.py` FastAPI app (`TRADING_MODE=paper` only)
- `app/config.py` pydantic-settings (SQLite URL, DexScreener base URL, scoring weight defaults as config, adapter flags)
- SQLAlchemy engine/session and models: `tokens`, `market_snapshots`, `ingestion_runs`
- `.env.example` with **no secrets** (no wallet fields)
- `GET /health` and `GET /meta` (paper mode, connected adapters, starting simulated balance $100)
- Pytest for health + DB smoke

Out of scope: scrapers, scores, dashboard UI.

## Stage 2 — DexScreener

**Goal:** real Solana pair discovery from DexScreener. No fake candles.

Deliverables:

- HTTPX adapter with timeouts, rate-limit handling, and `source=dexscreener`
- Job: fetch new/trending Solana pairs **and** persist snapshots
- Record token age, liquidity, volume, price, tx activity as **provided by the API**
- APIs: new launches, raw snapshots, ingestion run status
- Tests with **mocked** HTTP (no live network in CI)

Discovery lists are **ungated**. Safety gating comes in Stage 7.

**Snapshot history:** each scan appends `market_snapshots` and also refreshes tokens already in SQLite (feed lists rotate). While the backend is running, an in-process scheduler collects on `AUTO_DISCOVERY_INTERVAL_MINUTES` (default 15) when `AUTO_DISCOVERY_ENABLED=true`. Manual `POST /api/v1/discovery/scan` shares the same lock. See `docs/AUTO_DISCOVERY.md`.

## Stage 3 — News and RSS

- Configurable feed list
- Persist `news_items` + extracted entities
- Narrative freshness / item-velocity metrics
- Explicit `NOT_CONNECTED` if no feeds configured

## Stage 4 — Narratives

- Cluster news (and later social) into narratives
- Link to tokens only with stated method + confidence
- Surfaces: Breaking News, Viral Narratives, AI Coins/AI Narratives (AI slice is a narrative filter, not a separate invented feed)

## Stage 5 — Social integrations

Enable one adapter at a time when credentials/access exist:

1. Reddit public API if allowed
2. X/Twitter official/authorized API
3. Public Telegram sources with permitted access

Metrics: mention growth, engagement growth, unique accounts, acceleration, persistence, spam/coordination **estimates**. Unwired platforms stay `NOT_CONNECTED`.

## Milestone 5 Stage 1 — Opportunity ranking (implemented)

Explainable ranking over **stored** discovery, narrative, and security data.

- Engine: `app/services/opportunity_scoring.py`
- Persistence: append-only `opportunity_assessments`
- APIs: `POST /api/v1/opportunities/scan`, `GET /api/v1/opportunities`, `GET /api/v1/opportunities/{token_address}`, `GET /api/v1/opportunities/status`, history under `{token_address}/history`
- Formula, labels, and limitations: `docs/OPPORTUNITY_SCORING.md`
- Out of scope for this stage: paper trading, live orders, trained ML predictors, Stage 2 of Milestone 5

## Stage 6 — Momentum scoring

- Configurable weights (defaults 25/20/20/15/10/10)
- Component scores + confidence/completeness
- Lifecycle classifier: NEW, EARLY_DISCOVERY, EARLY_MOMENTUM, ACCELERATING, VIRAL, OVERHEATED, DECLINING, DEAD
- Explanation JSON on every signal
- Persist `signals` for later outcome labeling
- Safety veto hook (even if safety is still a stub: missing safety **lowers confidence**, it does not invent a green check)

## Stage 7 — Security and wallet intelligence

- Token safety checks as a **separate** pipeline from discovery
- Rug-pull risk as its own displayed score
- Helius/Birdeye adapters when keys exist
- Smart-money service: no “bought early ⇒ smart” rule
- High-Risk Tokens dashboard feed
- Critical safety **blocks** Early Opportunities

## Stage 8 — History and training

**Phase 1 (done):** market-only evaluation on stored snapshots (`docs/HISTORICAL_EVALUATION.md`). Horizons: next observation (~15 minutes) and 1 hour. Point-in-time features only.

Still later:

- 5-minute / 24-hour horizons
- Opportunity/narrative/security labels as signals
- Trained models and calibration
- Paper-trade fills (Stage 9)

## Stage 9 — Paper trading and backtesting

- Simulated $100 account
- Fill against stored/market snapshots, never chain submission
- Paper Trading Performance API
- Replay backtests on historical snapshots

## Stage 10 — SaaS

Only after the personal product is useful: auth, multi-tenant data, billing, rate limits, hosted Postgres. Do not introduce SaaS complexity in Stages 1–9.

## Immediate next implementation (awaiting approval)

Because this expansion asked to **report proposed changes before major architecture work**, the next code slice should be **Stage 1 only**:

1. FastAPI entrypoint + settings
2. SQLite + Stage 1 tables
3. Health/meta routes
4. Tests

Stage 2 DexScreener follows once Stage 1 is running.
