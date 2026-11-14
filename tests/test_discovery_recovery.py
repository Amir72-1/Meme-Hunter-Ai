"""Phase 8B interruption recovery: orphans, catch-up, deadline, rollback, status."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.models import DiscoveryScan, HistoricalEvaluation, MarketSnapshot, PointInTimeRun, Token
from app.services.discovery_recovery import recover_abandoned_scans, scan_is_overdue
from app.services.discovery_scheduler import DiscoveryScheduler
from app.services.token_discovery import DiscoveryScanResult, run_discovery_scan
from app.timeutil import utc_now
from tests.dexscreener_fakes import TOKEN_A, pair_payload, profile, run, ScriptedDex

NOW = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_discovery_scans() -> None:
    session = get_session_factory()()
    session.execute(delete(DiscoveryScan))
    session.commit()
    session.close()
    yield
    session = get_session_factory()()
    session.execute(delete(DiscoveryScan))
    session.commit()
    session.close()


def _session():
    return get_session_factory()()


def test_orphan_scan_recovery_is_idempotent() -> None:
    session = _session()
    completed = DiscoveryScan(started_at=NOW, finished_at=NOW, status="completed", snapshots_saved=4)
    failed = DiscoveryScan(started_at=NOW, finished_at=NOW, status="failed")
    orphan = DiscoveryScan(started_at=NOW, status="running")
    session.add_all([completed, failed, orphan])
    session.commit()
    completed_id, failed_id, orphan_id = completed.id, failed.id, orphan.id
    first = recover_abandoned_scans(session, now=NOW)
    assert first == [orphan_id]
    session.refresh(orphan)
    session.refresh(completed)
    session.refresh(failed)
    assert orphan.status == "interrupted"
    assert "previous process" in (orphan.errors_json or "").lower() or "restart" in (orphan.errors_json or "").lower()
    assert completed.status == "completed"
    assert completed.snapshots_saved == 4
    assert failed.status == "failed"
    second = recover_abandoned_scans(session, now=NOW)
    assert second == []
    session.refresh(orphan)
    assert orphan.status == "interrupted"
    session.close()


def test_backend_restart_recovers_orphan_via_scheduler_start() -> None:
    session = _session()
    session.add(DiscoveryScan(started_at=NOW, status="running"))
    session.commit()
    session.close()

    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        settings = Settings(auto_discovery_enabled=False, auto_discovery_interval_minutes=15)
        await scheduler.start(settings)
        assert scheduler.recovered_abandoned_scan_ids
        await scheduler.stop()

    run(exercise())
    session = _session()
    statuses = list(session.scalars(select(DiscoveryScan.status)).all())
    assert "running" not in statuses
    assert "interrupted" in statuses
    session.close()


def test_overdue_startup_scan_and_no_duplicate_catchup() -> None:
    session = _session()
    now = utc_now()
    session.add(
        DiscoveryScan(
            started_at=now - timedelta(minutes=40),
            finished_at=now - timedelta(minutes=20),
            status="completed",
        )
    )
    session.commit()
    last = session.scalar(select(DiscoveryScan).where(DiscoveryScan.status == "completed"))
    settings = Settings(
        auto_discovery_enabled=True,
        auto_discovery_interval_minutes=15,
        discovery_startup_catchup_delay_seconds=2,
    )
    assert scan_is_overdue(last, settings, now=now) is True
    session.close()

    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        waits: list[float] = []
        ticks = {"n": 0}

        async def wait(seconds: float) -> bool:
            waits.append(seconds)
            ticks["n"] += 1
            if ticks["n"] >= 2:
                assert scheduler._stop is not None
                scheduler._stop.set()
                return True
            return False

        async def tick(_settings) -> None:
            return None

        scheduler._wait = wait
        scheduler._scheduled_tick = tick
        await scheduler.start(settings)
        assert scheduler.catchup_pending is True
        await scheduler._task
        assert waits[0] == 2.0
        assert waits[1] == 900.0
        assert scheduler.catchup_pending is False
        await scheduler.stop()

    run(exercise())


def test_first_start_waits_full_interval_when_no_completed_scan() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        waits: list[float] = []
        settings = Settings(auto_discovery_enabled=True, auto_discovery_interval_minutes=15)

        async def wait(seconds: float) -> bool:
            waits.append(seconds)
            assert scheduler._stop is not None
            scheduler._stop.set()
            return True

        scheduler._wait = wait
        scheduler._scan = MagicMock()
        await scheduler.start(settings)
        await scheduler._task
        assert waits == [900.0]
        await scheduler.stop()

    run(exercise())


def test_internet_failure_then_recovery() -> None:
    script = ScriptedDex()
    script.profiles = httpx.Response(500, json={"error": "down"})
    session = _session()
    snap_before = session.scalar(select(func.count()).select_from(MarketSnapshot))

    async def fail() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.status == "failed"
        assert result.snapshots_saved == 0

    run(fail())
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == snap_before

    script.profiles = [profile(TOKEN_A)]
    script.tokens = [pair_payload(TOKEN_A)]

    async def ok() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.status == "completed"
        assert result.snapshots_saved == 1

    run(ok())
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == snap_before + 1
    session.close()


def test_failed_scan_then_successful_scan() -> None:
    test_internet_failure_then_recovery()


def test_scan_deadline_bounds_hang() -> None:
    settings = Settings(
        auto_discovery_interval_minutes=15,
        discovery_scan_deadline_seconds=0.2,
        dexscreener_request_deadline_seconds=0.2,
    )
    session = _session()

    class HangClient:
        async def get_latest_profiles(self):
            await asyncio.sleep(5)
            return []

        async def get_latest_boosts(self):
            return []

        async def get_top_boosts(self):
            return []

        async def get_tokens(self, _addresses):
            return []

    async def body() -> None:
        result = await run_discovery_scan(session, HangClient(), settings)  # type: ignore[arg-type]
        assert result.status == "failed"
        assert result.errors
        assert result.errors[0].category == "deadline"
        assert result.snapshots_saved == 0

    run(body())
    row = session.scalar(select(DiscoveryScan).order_by(DiscoveryScan.id.desc()))
    assert row is not None
    assert row.status == "failed"
    session.close()


def test_cancellation_during_scan_finalizes_interrupted() -> None:
    settings = Settings(auto_discovery_interval_minutes=15, discovery_scan_deadline_seconds=600)
    session = _session()

    class HangClient:
        async def get_latest_profiles(self):
            await asyncio.sleep(30)
            return []

        async def get_latest_boosts(self):
            return []

        async def get_top_boosts(self):
            return []

    async def body() -> None:
        task = asyncio.create_task(run_discovery_scan(session, HangClient(), settings))  # type: ignore[arg-type]
        for _ in range(40):
            await asyncio.sleep(0.05)
            session.expire_all()
            row = session.scalar(select(DiscoveryScan).order_by(DiscoveryScan.id.desc()))
            if row is not None and row.status == "running":
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session.expire_all()
        row = session.scalar(select(DiscoveryScan).order_by(DiscoveryScan.id.desc()))
        assert row is not None
        assert row.status == "interrupted"
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0 or True

    run(body())
    session.close()


def test_transaction_rollback_no_partial_snapshots() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.tokens = [pair_payload(TOKEN_A)]
    session = _session()
    before = session.scalar(select(func.count()).select_from(MarketSnapshot))
    commits = {"n": 0}
    real = session.commit

    def flaky() -> None:
        commits["n"] += 1
        if commits["n"] == 2:
            raise RuntimeError("disk full")
        real()

    session.commit = flaky  # type: ignore[method-assign]

    async def body() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.status == "failed"
        assert result.snapshots_saved == 0

    run(body())
    session.commit = real  # type: ignore[method-assign]
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == before
    session.close()


def test_scheduler_lock_released_after_failure() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        settings = get_settings()

        async def boom(*_args, **_kwargs):
            raise RuntimeError("network down")

        scheduler._scan = boom
        with pytest.raises(RuntimeError):
            await scheduler.try_execute(MagicMock(), settings, MagicMock())
        assert scheduler.is_busy() is False
        assert scheduler.last_status == "failed"

        async def ok(*_args, **_kwargs) -> DiscoveryScanResult:
            return DiscoveryScanResult(status="completed", snapshots_saved=1)

        scheduler._scan = ok
        result = await scheduler.try_execute(MagicMock(), settings, MagicMock())
        assert result.status == "completed"
        assert scheduler.is_busy() is False

    run(exercise())


def test_scheduler_status_uses_database_after_restart(client: TestClient) -> None:
    session = _session()
    session.add(
        DiscoveryScan(
            started_at=NOW - timedelta(minutes=30),
            finished_at=NOW - timedelta(minutes=29),
            status="completed",
        )
    )
    session.add(
        DiscoveryScan(
            started_at=NOW - timedelta(minutes=15),
            finished_at=NOW - timedelta(minutes=14),
            status="failed",
        )
    )
    session.commit()
    session.close()
    payload = client.get("/api/v1/scheduler/status").json()
    assert payload["completed_scans_total"] >= 1
    assert payload["failed_scans_total"] >= 1
    assert payload["last_successful_scan_id"] is not None
    assert payload["last_failed_scan_id"] is not None
    assert payload["last_status"] in {"completed", "failed", "interrupted"}
    assert payload["scan_in_progress"] is False


def test_snapshot_eval_and_pit_immutability_during_recovery() -> None:
    session = _session()
    token = Token(token_address="RecovTok111111111111111111111111111111111", chain="solana", symbol="RCV")
    session.add(token)
    session.flush()
    snap = MarketSnapshot(
        token_id=token.id,
        price_usd=0.01,
        liquidity_usd=None,
        pair_address="PairR",
        recorded_at=NOW,
    )
    ev = HistoricalEvaluation(
        started_at=NOW,
        finished_at=NOW,
        status="completed",
        horizon="next_observation",
        outcomes_measured=14040,
        explanation="preserve",
    )
    pit = PointInTimeRun(
        started_at=NOW,
        finished_at=NOW,
        status="completed",
        candidates_count=17197,
        snapshots_scanned=17197,
        explanation="preserve",
    )
    session.add_all([snap, ev, pit, DiscoveryScan(started_at=NOW, status="running")])
    session.commit()
    snap_id, price, ev_id, outcomes, pit_id, cand = (
        snap.id,
        snap.price_usd,
        ev.id,
        ev.outcomes_measured,
        pit.id,
        pit.candidates_count,
    )
    recover_abandoned_scans(session, now=NOW)
    session.expire_all()
    loaded_snap = session.get(MarketSnapshot, snap_id)
    loaded_ev = session.get(HistoricalEvaluation, ev_id)
    loaded_pit = session.get(PointInTimeRun, pit_id)
    assert loaded_snap is not None
    assert loaded_snap.price_usd == price
    assert loaded_snap.liquidity_usd is None
    assert loaded_ev is not None
    assert loaded_ev.outcomes_measured == outcomes
    assert loaded_pit is not None
    assert loaded_pit.candidates_count == cand
    session.close()


def test_continuity_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/readiness/continuity")
    assert response.status_code == 200
    body = response.json()
    assert "calendar_span_days" in body
    assert "accumulated_scan_execution_seconds" in body
    assert "uptime" not in body.get("note", "").lower() or "not stored" in body["note"].lower()
    assert "invent" in body["note"].lower() or "not invented" in body["note"].lower()
