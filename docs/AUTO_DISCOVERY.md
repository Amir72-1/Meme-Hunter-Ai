# Automatic market-data collection

While the FastAPI backend is running, MEME HUNTER AI can collect Solana DexScreener snapshots on a timer. The scheduler reuses `run_discovery_scan` (same filters, stored-token refresh, and append-only `market_snapshots`). It does **not** call the HTTP scan endpoint.

## Enable or disable

In `.env` (copy from `.env.example`; do not commit secrets):

```
AUTO_DISCOVERY_ENABLED=true
AUTO_DISCOVERY_INTERVAL_MINUTES=15
```

- `true` / `false` — start or skip the background task at process startup.
- Interval must be greater than 0 and at most 1440 minutes (24 hours).
- If these keys are missing, defaults are enabled and 15 minutes.

Restart the backend after changing `.env`.

## How it runs

1. FastAPI lifespan starts **one** asyncio task in this process.
2. Startup recovery marks leftover `discovery_scans.status=running` rows as `interrupted`. Completed and failed rows are not changed.
3. **First-start policy:** if there is no prior completed scan, wait **one full interval** before the first scan.
4. **Overdue catch-up:** if the last completed scan is older than the interval (power loss, crash, closed terminal), wait `DISCOVERY_STARTUP_CATCHUP_DELAY_SECONDS` (default 2s) and run **one** current-time scan. Offline minutes are not backfilled. After that, the 15-minute cadence resumes.
5. Each scan has a hard deadline (`DISCOVERY_SCAN_DEADLINE_SECONDS`, default 600s, always below the interval). DNS/HTTP hangs cannot hold the lock past that deadline.
6. If a scan is already running (manual or scheduled), the tick is **skipped**.
7. Unexpected errors, cancel, and deadline roll back uncommitted snapshots and finalize the scan row. Prior committed snapshots stay.
8. Lifespan shutdown cancels the task. Closing the terminal or stopping uvicorn **stops collection**.

There is no Windows service. After electricity or a reboot, start uvicorn again (see below). Do not use `--workers` greater than 1.

## Inspect status

`GET /api/v1/scheduler/status` — process state plus **SQLite** completed/failed totals, last success/fail, last duration, consecutive failures.

`GET /api/v1/readiness/continuity` — calendar span vs accumulated scan execution vs gaps (not OS uptime).

In-memory tick counters still reset on restart; historical totals do not.

## Single-process limitation

Overlap locking uses an in-process `asyncio.Lock` shared by `POST /api/v1/discovery/scan` and the scheduler. That is **not** multi-worker coordination.

Do **not** run `uvicorn --workers N` with N > 1. `--reload` is one child process at a time; a reload cancels the task and starts a new one after another full interval wait.

## Start the backend

```
cd meme-hunter-ai
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Then `GET http://127.0.0.1:8000/api/v1/scheduler/status` — `enabled` and `running` should be true when auto-discovery is on. After the interval, `successful_runs` or `failed_runs` increments and snapshot counts on tokens increase.
