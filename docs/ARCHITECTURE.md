# MEME HUNTER AI — Architecture

Status: Stage 1–5 paper intelligence is implemented in this tree. Automatic DexScreener collection is an in-process asyncio task started from FastAPI lifespan (`docs/AUTO_DISCOVERY.md`). Sections below that still say “proposed / empty folders” are historical notes, not current inventory.

MEME HUNTER AI is a personal, paper-trading-only platform for **early** Solana memecoin discovery. The product goal is not “show coins that are already trending.” It is to detect **emerging narratives**, **viral attention**, and **early on-chain activity** as soon as the available data supports it — then explain the evidence, the gaps, and the risks.

## Current codebase (inspected 2026-11-01)

Working tree under `meme-hunter-ai/`:

| Path | State |
|------|--------|
| `.venv/` | Python 3.11 venv with FastAPI, Uvicorn, HTTPX, SQLAlchemy, pydantic-settings, python-dotenv, pytest |
| `requirements.txt` | Backend dependencies only |
| `.gitignore` | Python, `.venv/`, `.env`, `*.db`, `.pytest_cache/` |
| `app/`, `app/api/`, `app/services/` | Empty package folders |
| `tests/`, `docs/` | Empty except this architecture and roadmap |

There is **no FastAPI app, no database, no DexScreener client, and no dashboard** yet. Nothing below is claimed as live.

## Product constraints (non-negotiable)

- Paper trading only. Starting simulated balance: **$100**.
- No real wallet connect, seed phrases, private keys, or live transactions.
- No fabricated quotes, mentions, wallet labels, or scores.
- Every signal stores **what was measured**, **what was estimated**, and **what was missing**.
- A high opportunity score **cannot** override a critical safety warning.
- Predictive models (later) may only train on information that existed at prediction time.

## Design principles

1. **Early ≠ trending.** Discovery looks for freshness, acceleration, and narrative birth, not already-viral coins.
2. **Adapters over assumptions.** Each external source is a pluggable adapter. Unconnected sources return `NOT_CONNECTED`, never dummy trends.
3. **Safety is a gate, not a spice.** Token discovery and token safety are separate pipelines. Safety can veto promotion to Early Opportunities.
4. **Explainability first.** Every score and lifecycle stage must cite inputs, missing data, and invalidation conditions.
5. **Learn from history.** Raw observations and issued signals are stored so later models can be scored against outcomes without lookahead.

## Proposed system shape

```text
                    ┌──────────────────────────────┐
                    │  Collectors (scheduled jobs) │
                    │  news / social / market /    │
                    │  (future) on-chain           │
                    └──────────────┬───────────────┘
                                   │ normalized events
                                   v
┌──────────────┐    ┌──────────────────────────────────────────┐
│ SQLite       │<───│  Intelligence services                    │
│ observations │    │  narratives · discovery · virality        │
│ signals      │    │  wallets · scoring · lifecycle            │
│ outcomes     │    └──────────────────────┬───────────────────┘
└──────────────┘                           │
                                           v
                    ┌──────────────────────────────────────────┐
                    │  FastAPI (read APIs + paper trading)     │
                    │  Dashboard is a later consumer           │
                    └──────────────────────────────────────────┘
```

## Proposed package layout (Stage 1 onward)

Do not flatten this all in one commit. Add packages as each stage lands.

```text
app/
  main.py                 # FastAPI app, health, paper-trading mode banner
  config.py               # pydantic-settings; scoring weights; feature flags
  db/
    session.py
    models.py             # SQLAlchemy tables
  api/                    # HTTP routers
  services/
    discovery/            # DexScreener new pairs, volume, liquidity
    news/                 # RSS/news ingest + entity/topic extraction
    narratives/           # link news/social topics to tokens
    virality/             # social adapters + acceleration metrics
    wallets/              # modular smart-money analysis (stubs first)
    scoring/              # 0–100 explainable scores + veto rules
    lifecycle/            # NEW … DEAD classification
    paper_trading/        # simulated $100 book, no chain submit
    outcomes/             # later: label signals vs future price/volume
  adapters/
    dexscreener.py
    rss.py
    x_twitter.py          # disabled until credentials exist
    reddit.py
    telegram.py
    helius.py             # future
    birdeye.py            # future
tests/
docs/
```

## Data model (Stage 1 core + later tables)

Stage 1 should create SQLite and the tables needed for market ingest. Later stages add the rest without rewriting Stage 1 tables.

### Stage 1

- `tokens` — mint, symbol, name, pair address, first_seen_at, source
- `market_snapshots` — price, liquidity, volume, tx counts, market cap, captured_at, source
- `ingestion_runs` — adapter, started_at, finished_at, status, item_count, error

### Later stages

- `news_items` — source, url, published_at, title, body excerpt, fetched_at
- `news_entities` — item_id, entity, entity_type, salience
- `narratives` — canonical topic, freshness, acceleration, first_seen_at
- `narrative_token_links` — narrative_id, token_id, evidence, confidence, method (`exact_ticker` / `name_match` / `manual` / `model`)
- `social_snapshots` — platform, topic_or_token, mentions, engagement, unique_accounts, captured_at, source_status
- `wallet_observations` — wallet, token, action, size, observed_at, source (`NOT_CONNECTED` until Helius/Birdeye)
- `signals` — token_id, issued_at, component scores, composite, lifecycle, explanation JSON, data_completeness
- `signal_outcomes` — signal_id, horizon, forward return/volume, false_positive, missed_opportunity flags
- `paper_accounts` / `paper_orders` / `paper_positions` — simulated $100 book

Every snapshot and signal should store:

- `measured` vs `estimated` vs `unavailable`
- `source`
- `captured_at` (observation time, not insert time)

## External adapters

| Adapter | Stage | Default status | Notes |
|---------|-------|----------------|--------|
| DexScreener | 2 | **connected** (public HTTP) | New Solana pairs, liquidity, volume. Primary market source. |
| RSS / public news | 3 | **connected** if URLs configured | Breaking news, celebrities, politics, sports, tech, AI, gaming, culture. |
| X/Twitter | 5 | `NOT_CONNECTED` | Official API or authorized access only. |
| Reddit | 5 | `NOT_CONNECTED` until client configured | Public subreddit search/listings. |
| Telegram | 5 | `NOT_CONNECTED` | Only public sources with permitted access. |
| Helius / Birdeye | 7 | `NOT_CONNECTED` | Wallet and richer on-chain metrics. |

Unconnected adapters must not emit synthetic “0 mentions” that look like a real measurement. They emit `status=NOT_CONNECTED` and scoring treats that as **missing data**, which lowers confidence.

## Intelligence modules

### News intelligence

- Poll configured RSS/Atom feeds on a schedule.
- Extract entities, keywords, topics.
- Score **narrative freshness** (age since first mention) and **attention velocity** (item rate vs recent baseline).
- Flag events that historically seed meme demand (celebrity, politics, sports outcomes, AI product launches, gaming drops, internet culture).
- Link narratives to tokens only with an explicit method and confidence. No silent fuzzy guessing presented as fact.

### Virality

Modular metrics, computed only on connected platforms:

- Mention growth
- Engagement growth
- Independent-account count
- Trend acceleration
- Community activity
- Narrative persistence
- Spam / bot / coordination heuristics (conservative; labeled as estimates)

### Early token discovery (DexScreener first)

Track new mints/pairs, new liquidity, volume acceleration, buyer/seller pressure where the API provides it, liquidity changes, early market-cap movement, token age, and trading activity.

Discovery **does not** equal safety approval. A brand-new pair can appear in New Launches while remaining excluded from Early Opportunities.

### Smart-money intelligence

A **stub service** until Helius or Birdeye is wired. Future analysis:

- Early buyers, trade history, verifiable profitability
- Repeated successful early entries
- Developer / insider activity
- Concentration and coordinated flow

Never label a wallet “smart money” only because it bought early.

### Scoring (0–100, configurable weights)

Initial defaults (must live in settings, not hardcoded forever):

| Component | Weight |
|-----------|--------|
| Market momentum | 25% |
| Social virality | 20% |
| News / narrative | 20% |
| On-chain wallet signals | 15% |
| Liquidity / trading quality | 10% |
| Token safety | 10% |

Displayed separately:

- Early opportunity
- Virality
- Narrative strength
- Market momentum
- Token safety
- Rug-pull risk
- Confidence / data completeness

**Veto rule:** if token safety is critical or rug-pull risk is critical, Early Opportunities must not promote the token regardless of composite score. The API still returns the scores with `blocked_by_safety=true`.

Never claim a token will pump. Language is “signal,” “evidence,” “risk,” “incomplete data.”

### Lifecycle

`NEW` → `EARLY_DISCOVERY` → `EARLY_MOMENTUM` → `ACCELERATING` → `VIRAL` → `OVERHEATED` → `DECLINING` → `DEAD`

Each transition stores the rule that fired (age, volume acceleration, social velocity, liquidity, etc.). Stages are descriptive, not buy advice.

### Explanations

Every token payload includes:

- Why it was discovered
- Linked narrative (or none)
- Supporting news/social evidence
- Attention accelerating vs declining
- On-chain evidence (or `NOT_CONNECTED`)
- What would invalidate the signal
- Main risks and missing data

## Dashboard (later UI consumer)

Backend should eventually expose list endpoints that map 1:1 to:

- Early Opportunities (safety-gated)
- Breaking News
- Viral Narratives
- New Solana Launches
- AI Coins and AI Narratives
- Smart Money Activity
- Overheated Tokens
- High-Risk Tokens
- Paper Trading Performance

UI is not in Stage 1.

## Safety and trading

- Simulated ledger only. No signing, no RPC sendTransaction, no wallet adapters.
- Config `TRADING_MODE=paper` is required; any other value refuses to start.
- Do not request or persist seed phrases or private keys.

## What will not be built in the next implementation pass

Per the staged roadmap: **do not implement news, social, scoring, wallets, or paper trading until Stage 1–2 foundations exist.** The next approved implementation slice is Stage 1 (app + SQLite) then Stage 2 (DexScreener), described in `docs/ROADMAP.md`.
