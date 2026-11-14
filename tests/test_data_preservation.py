"""Stage 2B.1.2: append-only catalog, test isolation, backups, collection identity."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.data_protection import (
    DESTRUCTIVE_CONFIRMATION,
    DestructiveOperationBlocked,
    assert_engine_reset_allowed,
    assert_safe_test_database_path,
    database_identity,
    install_append_only_guards,
    is_ephemeral_test_database,
    is_protected_catalog,
    remove_append_only_guards,
    sqlite_file_from_url,
)
from app.database import Base
from app.models import DiscoveryScan, MarketSnapshot, Token
from app.services.data_readiness import assemble_readiness
from app.services.discovery_recovery import collection_continuity
from app.services.discovery_scheduler import DiscoveryScheduler
from app.services.sqlite_backup import (
    backup_configured_database,
    copy_sqlite_online,
    prune_backup_files,
    should_skip_backup,
    verify_sqlite_integrity,
)
from app.services.token_discovery import insert_snapshot, upsert_token
from app.services.market_data import NormalizedMarket
from tests.conftest import TEST_DB_PATH
from tests.test_data_readiness import _Sched

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _market(address: str, at: datetime, price: float = 1.0) -> NormalizedMarket:
    return NormalizedMarket(
        token_address=address,
        chain="solana",
        name="Tok",
        symbol="TOK",
        dex_name="pumpfun",
        pair_address="Pair111111111111111111111111111111111111111",
        pair_created_at=at,
        price_usd=price,
        market_cap=1000,
        liquidity_usd=20000,
        volume_24h=5000,
        volume_5m=10,
        volume_1h=100,
        price_change_5m=0.1,
        price_change_1h=0.2,
        price_change_24h=0.3,
        fdv=1000,
        buys_5m=1,
        sells_5m=1,
        buys_1h=2,
        sells_1h=2,
        buys_24h=3,
        sells_24h=3,
        active_boosts=0,
        discovery_source="test",
        filter_status="passed",
        filter_reasons="",
        snapshot_at=at,
    )


def test_test_database_is_isolated_from_production() -> None:
    url = f"sqlite:///{TEST_DB_PATH.as_posix()}"
    assert is_ephemeral_test_database(url)
    assert not is_protected_catalog(url)
    assert sqlite_file_from_url(url) == TEST_DB_PATH.resolve()
    identity = database_identity(url)
    assert identity["filename"] == "test_meme_hunter.db"
    assert identity["protected_catalog"] is False
    assert_safe_test_database_path(TEST_DB_PATH)
    with pytest.raises(DestructiveOperationBlocked, match="DESTRUCTIVE_OP_BLOCKED"):
        assert_safe_test_database_path(TEST_DB_PATH.parent / "meme_hunter.db")
    with pytest.raises(DestructiveOperationBlocked, match="DESTRUCTIVE_OP_BLOCKED"):
        assert_engine_reset_allowed("sqlite:///./meme_hunter.db")


def test_snapshot_insert_is_append_only(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'catalog.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    first = _market("MintAppend11111111111111111111111111111111", NOW, 1.0)
    upsert_token(session, first)
    insert_snapshot(session, first)
    second = _market("MintAppend11111111111111111111111111111111", NOW + timedelta(minutes=15), 1.1)
    upsert_token(session, second)
    insert_snapshot(session, second)
    session.commit()
    snaps = list(session.scalars(select(MarketSnapshot).order_by(MarketSnapshot.id)).all())
    tokens = list(session.scalars(select(Token)).all())
    assert len(tokens) == 1
    assert len(snaps) == 2
    assert snaps[0].price_usd == 1.0
    assert snaps[1].price_usd == 1.1
    session.close()
    engine.dispose()


def test_protected_catalog_blocks_snapshot_delete(tmp_path: Path) -> None:
    path = tmp_path / "meme_hunter.db"
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    install_append_only_guards(engine, url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    market = _market("MintProtect1111111111111111111111111111111", NOW)
    upsert_token(session, market)
    insert_snapshot(session, market)
    session.commit()
    snap_id = session.scalar(select(MarketSnapshot.id))
    with pytest.raises(Exception, match="DESTRUCTIVE_OP_BLOCKED"):
        session.execute(delete(MarketSnapshot))
        session.commit()
    session.rollback()
    assert session.get(MarketSnapshot, snap_id) is not None
    with pytest.raises(DestructiveOperationBlocked, match="DESTRUCTIVE_OP_BLOCKED"):
        remove_append_only_guards(engine, url, "nope")
    remove_append_only_guards(engine, url, DESTRUCTIVE_CONFIRMATION)
    session.execute(delete(MarketSnapshot))
    session.commit()
    assert session.scalar(select(MarketSnapshot)) is None
    session.close()
    engine.dispose()


def test_backup_skips_test_database(monkeypatch) -> None:
    monkeypatch.setenv("SQLITE_BACKUP_ENABLED", "true")
    settings = Settings(
        database_url=f"sqlite:///{TEST_DB_PATH.as_posix()}",
        sqlite_backup_enabled=True,
        sqlite_backup_dir="./data/backups",
    )
    assert should_skip_backup(settings.database_url, settings) == "ephemeral_test_database"
    result = backup_configured_database(settings.database_url, settings)
    assert result.status == "skipped"
    assert result.skipped_reason == "ephemeral_test_database"


def test_backup_integrity_and_retention(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SQLITE_BACKUP_ENABLED", "true")
    source = tmp_path / "catalog.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO items(value) VALUES ('alpha')")
    conn.commit()
    conn.close()
    dest_dir = tmp_path / "backups"
    settings = Settings(
        database_url=f"sqlite:///{source.as_posix()}",
        sqlite_backup_enabled=True,
        sqlite_backup_dir=str(dest_dir),
        sqlite_backup_retain=2,
        sqlite_backup_min_interval_seconds=0,
    )
    first = backup_configured_database(settings.database_url, settings, now=NOW)
    assert first.status == "completed"
    assert first.integrity == "ok"
    assert first.backup_path
    assert verify_sqlite_integrity(Path(first.backup_path)) == "ok"
    check = sqlite3.connect(first.backup_path)
    assert check.execute("select value from items").fetchone()[0] == "alpha"
    check.close()
    second = backup_configured_database(
        settings.database_url, settings, now=NOW + timedelta(minutes=1)
    )
    third = backup_configured_database(
        settings.database_url, settings, now=NOW + timedelta(minutes=2)
    )
    assert second.status == "completed"
    assert third.status == "completed"
    remaining = list(dest_dir.glob("meme_hunter_*.db"))
    assert len(remaining) == 2


def test_backup_while_writes_occur(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE ticks (id INTEGER PRIMARY KEY, n INTEGER)")
    conn.commit()
    conn.close()

    stop = threading.Event()

    def writer() -> None:
        local = sqlite3.connect(source, timeout=30)
        n = 0
        while not stop.is_set():
            n += 1
            local.execute("INSERT INTO ticks(n) VALUES (?)", (n,))
            local.commit()
            time.sleep(0.01)
        local.close()

    thread = threading.Thread(target=writer)
    thread.start()
    time.sleep(0.05)
    dest = tmp_path / "copy.db"
    try:
        copy_sqlite_online(source, dest)
        assert verify_sqlite_integrity(dest) == "ok"
        copied = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
        count = copied.execute("select count(*) from ticks").fetchone()[0]
        copied.close()
        assert count >= 1
    finally:
        stop.set()
        thread.join(timeout=5)


def test_prune_never_deletes_live_catalog(tmp_path: Path) -> None:
    live = tmp_path / "meme_hunter_live.db"
    live.write_bytes(b"sqlite")
    old = tmp_path / "meme_hunter_20000101T000000Z.db"
    old.write_bytes(b"old")
    newer = tmp_path / "meme_hunter_20260101T000000Z.db"
    newer.write_bytes(b"new")
    removed = prune_backup_files(tmp_path, retain=1, live_path=live)
    assert str(old) in removed
    assert live.exists()


def test_collection_gap_detection() -> None:
    scans = [
        DiscoveryScan(
            id=1,
            started_at=NOW - timedelta(hours=3),
            finished_at=NOW - timedelta(hours=3) + timedelta(seconds=8),
            status="completed",
        ),
        DiscoveryScan(
            id=2,
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(hours=1) + timedelta(seconds=8),
            status="completed",
        ),
        DiscoveryScan(
            id=3,
            started_at=NOW - timedelta(minutes=10),
            finished_at=NOW - timedelta(minutes=9),
            status="failed",
        ),
    ]
    payload = collection_continuity(scans, [], expected_interval_minutes=15, now=NOW)
    assert payload["completed_scans_total"] == 2
    assert payload["failed_scans_total"] == 1
    assert payload["longest_gap_between_completed_scans_minutes"] == pytest.approx(120.0 - 8 / 60, rel=1e-3)
    report = assemble_readiness(
        tokens=[],
        snapshots=[],
        discovery_scans=scans,
        risk_rows=[],
        security_scans=[],
        narrative_matches=[],
        opportunities=[],
        historical_evals=[],
        quality_evals=[],
        pit_run=None,
        pit_candidates=[],
        settings=get_settings(),
        scheduler=_Sched(enabled=True, running=True),
        now=NOW,
        database_identity={"resolved_path": str(TEST_DB_PATH)},
        last_successful_backup=None,
        last_backup={"status": "failed", "error": "disk"},
    )
    health = report["collection_health"]
    assert health["snapshot_count"] == 0
    assert health["tokens_with_history"] == 0
    assert health["database_identity"]["resolved_path"] == str(TEST_DB_PATH)
    assert health["last_backup"]["status"] == "failed"
    assert health["collection_gaps"]["failed_scans_total"] == 1


def test_scheduler_restart_recovers_without_second_task() -> None:
    async def exercise() -> None:
        scheduler = DiscoveryScheduler()
        settings = Settings(auto_discovery_enabled=True, auto_discovery_interval_minutes=15)
        await scheduler.start(settings)
        first = scheduler._task
        await scheduler.start(settings)
        assert scheduler._task is first
        await scheduler.stop()
        await scheduler.start(settings)
        assert scheduler.running is True
        await scheduler.stop()

    import asyncio

    asyncio.run(exercise())
