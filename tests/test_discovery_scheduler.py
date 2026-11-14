"""Automatic discovery scheduler: overlap, wait, shutdown, and UTC snapshots."""

from __future__ import annotations

from asyncio import Event, create_task, run
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.main import create_app
from app.models import DiscoveryScan, MarketSnapshot, Token
from app.services.discovery_scheduler import (
    DiscoveryScheduler,
    get_scheduler,
    reset_scheduler,
    sanitize_error,
)
from app.services.opportunity_scoring import snapshots_as_of
from app.services.token_discovery import DiscoveryScanResult, run_discovery_scan
from tests.dexscreener_fakes import TOKEN_A, pair_payload, profile, ScriptedDex

TOKEN_SCHED = "SchedTok11111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def _clean_scans() -> None:
    session = get_session_factory()()
    session.execute(delete(DiscoveryScan))
    session.commit()
    session.close()
    yield
    session = get_session_factory()()
    session.execute(delete(DiscoveryScan))
    session.commit()
    session.close()


def test_scheduler_status_disabled(client: TestClient) -> None:
    response = client.get("/api/v1/scheduler/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["running"] is False
    assert payload["successful_runs"] == 0
    assert payload["last_status"] == "never"
    assert "single-process" in payload["note"].lower() or "single-process" in payload["note"]
    assert "wallet" not in payload["note"].lower()


def test_scheduler_starts_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("AUTO_DISCOVERY_INTERVAL_MINUTES", "0.05")
    get_settings.cache_clear()
    reset_scheduler()
    application = create_app()
    try:
        with TestClient(application) as test_client:
            payload = test_client.get("/api/v1/scheduler/status").json()
            assert payload["enabled"] is True
            assert payload["running"] is True
            assert payload["interval_minutes"] == 0.05
            assert payload["next_run_at"] is not None
            assert payload["successful_runs"] == 0
    finally:
        get_settings.cache_clear()
        reset_scheduler()


def test_invalid_interval_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(auto_discovery_interval_minutes=0)
    with pytest.raises(ValidationError):
        Settings(auto_discovery_interval_minutes=-5)
    with pytest.raises(ValidationError):
        Settings(auto_discovery_interval_minutes=1441)


def test_first_scan_waits_for_interval() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        waits: list[float] = []
        settings = Settings(auto_discovery_enabled=True, auto_discovery_interval_minutes=15)

        async def wait(seconds: float) -> bool:
            waits.append(seconds)
            assert scheduler._stop is not None
            scheduler._stop.set()
            return True

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("scan must not run before the first interval")

        scheduler._wait = wait
        scheduler._scan = forbidden
        await scheduler.start(settings)
        assert scheduler.running is True
        await scheduler._task
        assert waits == [900.0]
        assert scheduler.successful_runs == 0
        await scheduler.stop()

    run(exercise())


def test_successful_scan_updates_status() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        ticks = {"n": 0}
        settings = Settings(auto_discovery_enabled=True, auto_discovery_interval_minutes=1)

        async def wait(_seconds: float) -> bool:
            ticks["n"] += 1
            if ticks["n"] == 1:
                return False
            assert scheduler._stop is not None
            scheduler._stop.set()
            return True

        async def ok(*_args, **_kwargs) -> DiscoveryScanResult:
            return DiscoveryScanResult(status="completed", snapshots_saved=2)

        scheduler._wait = wait
        scheduler._scheduled_tick = lambda _settings: scheduler.try_execute(
            MagicMock(), settings, MagicMock()
        )
        scheduler._scan = ok
        await scheduler.start(settings)
        await scheduler._task
        assert scheduler.successful_runs == 1
        assert scheduler.last_status == "completed"
        assert scheduler.last_error is None
        assert scheduler.last_started_at is not None
        assert scheduler.last_finished_at is not None
        await scheduler.stop()

    run(exercise())


def test_failed_scan_does_not_stop_scheduler() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        ticks = {"n": 0}
        settings = Settings(auto_discovery_enabled=True, auto_discovery_interval_minutes=1)

        async def wait(_seconds: float) -> bool:
            ticks["n"] += 1
            if ticks["n"] >= 3:
                assert scheduler._stop is not None
                scheduler._stop.set()
                return True
            return False

        async def boom(_settings) -> None:
            raise RuntimeError("DexScreener timeout")

        scheduler._wait = wait
        scheduler._scheduled_tick = boom
        await scheduler.start(settings)
        await scheduler._task
        assert scheduler.failed_runs >= 1
        assert scheduler.last_status == "failed"
        assert scheduler.last_error is not None
        assert "timeout" in scheduler.last_error.lower()
        await scheduler.stop()
        assert scheduler.running is False

    run(exercise())


def test_running_scan_skips_overlapping_scheduled_and_manual() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        started = Event()
        release = Event()
        settings = get_settings()

        async def slow(*_args, **_kwargs) -> DiscoveryScanResult:
            started.set()
            await release.wait()
            return DiscoveryScanResult(status="completed", snapshots_saved=1)

        scheduler._scan = slow
        first = create_task(scheduler.try_execute(MagicMock(), settings, MagicMock()))
        await started.wait()
        skipped = await scheduler.try_execute(MagicMock(), settings, MagicMock())
        assert skipped.status == "skipped"
        assert skipped.snapshots_saved == 0
        assert scheduler.skipped_runs == 1
        release.set()
        completed = await first
        assert completed.status == "completed"
        assert scheduler.successful_runs == 1

    run(exercise())


def test_manual_scan_returns_conflict_when_busy(client: TestClient, monkeypatch) -> None:
    scheduler = get_scheduler()
    monkeypatch.setattr(scheduler, "is_busy", lambda: True)
    response = client.post("/api/v1/discovery/scan")
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "skipped"
    assert body["snapshots_saved"] == 0


def test_shutdown_cancels_background_task() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        settings = Settings(auto_discovery_enabled=True, auto_discovery_interval_minutes=15)

        async def wait(_seconds: float) -> bool:
            assert scheduler._stop is not None
            await scheduler._stop.wait()
            return True

        scheduler._wait = wait
        await scheduler.start(settings)
        assert scheduler.running is True
        await scheduler.stop()
        assert scheduler.running is False
        assert scheduler._task is None

    run(exercise())


def test_restart_does_not_create_duplicate_tasks() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        settings = Settings(auto_discovery_enabled=True, auto_discovery_interval_minutes=15)

        async def wait(_seconds: float) -> bool:
            assert scheduler._stop is not None
            await scheduler._stop.wait()
            return True

        scheduler._wait = wait
        await scheduler.start(settings)
        first = scheduler._task
        await scheduler.start(settings)
        assert scheduler._task is first
        await scheduler.stop()

    run(exercise())


def test_error_message_is_sanitized() -> None:
    cleaned = sanitize_error("failed token=abcd1234 secret=super")
    assert "super" not in cleaned
    assert "[redacted]" in cleaned


def test_historical_snapshots_stay_append_only() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_SCHED)]
    script.latest_boosts = []
    script.top_boosts = []
    script.tokens = [pair_payload(TOKEN_SCHED, priceUsd="0.02")]
    session = get_session_factory()()
    scheduler = DiscoveryScheduler()

    async def scan() -> None:
        async with script.client() as client:
            await scheduler.try_execute(session, get_settings(), client)

    run(scan())
    token = session.scalar(select(Token).where(Token.token_address == TOKEN_SCHED))
    assert token is not None
    first = list(
        session.scalars(
            select(MarketSnapshot).where(MarketSnapshot.token_id == token.id).order_by(MarketSnapshot.id)
        ).all()
    )
    assert len(first) == 1
    first_id = first[0].id
    first_recorded = first[0].recorded_at
    script.tokens = [pair_payload(TOKEN_SCHED, priceUsd="0.03")]
    run(scan())
    rows = list(
        session.scalars(
            select(MarketSnapshot).where(MarketSnapshot.token_id == token.id).order_by(MarketSnapshot.id)
        ).all()
    )
    assert len(rows) == 2
    assert rows[0].id == first_id
    assert rows[0].recorded_at == first_recorded
    assert rows[0].price_usd == 0.02
    assert rows[1].price_usd == 0.03
    session.close()


def test_utc_timestamps_are_aware_and_future_rows_are_excluded() -> None:
    session = get_session_factory()()
    token = Token(token_address="UtcTest111111111111111111111111111111111", chain="solana")
    session.add(token)
    session.flush()
    naive = datetime(2026, 11, 1, 12, 0, 0)
    session.add(MarketSnapshot(token_id=token.id, price_usd=1.0, recorded_at=naive))
    session.commit()
    loaded = session.scalar(
        select(MarketSnapshot).where(MarketSnapshot.token_id == token.id)
    )
    assert loaded is not None
    assert loaded.recorded_at.tzinfo is not None
    assert loaded.recorded_at.utcoffset() is not None
    assert loaded.recorded_at.utcoffset().total_seconds() == 0
    as_of = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert len(snapshots_as_of([loaded], as_of)) == 1
    earlier = datetime(2026, 11, 1, 11, 0, 0, tzinfo=timezone.utc)
    assert snapshots_as_of([loaded], earlier) == []
    session.close()


def test_direct_discovery_scan_still_appends(monkeypatch) -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.tokens = [pair_payload(TOKEN_A, priceUsd="0.10")]
    session = get_session_factory()()

    async def scan() -> None:
        async with script.client() as client:
            await run_discovery_scan(session, client)

    run(scan())
    assert session.scalar(select(MarketSnapshot).where(MarketSnapshot.price_usd == 0.10)) is not None
    session.close()
