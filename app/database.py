"""SQLAlchemy engine, session factory, and connectivity helpers."""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

logger = logging.getLogger("meme_hunter.database")


class UtcDateTime(TypeDecorator):
    """Store timestamps as UTC. Naive SQLite values are interpreted as UTC on load."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _engine_kwargs(database_url: str) -> dict[str, object]:
    kwargs: dict[str, object] = {"future": True}
    if database_url.startswith("sqlite"):
        # FastAPI can access SQLite from multiple threads in one process.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return kwargs


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    return create_engine(url, **_engine_kwargs(url))


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return _SessionLocal


def reset_engine(database_url: str | None = None) -> Engine:
    """Replace the process-wide engine. Used by tests."""
    from app.data_protection import assert_engine_reset_allowed

    global _engine, _SessionLocal
    url = database_url or get_settings().database_url
    assert_engine_reset_allowed(url)
    if _engine is not None:
        _engine.dispose()
    _engine = create_db_engine(url)
    _SessionLocal = sessionmaker(
        bind=_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    return _engine


def get_db() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


# Additive SQLite columns for existing development databases. Never DROP.
_TOKEN_COLUMNS: dict[str, str] = {
    "dex_name": "VARCHAR(64)",
    "pair_address": "VARCHAR(88)",
    "pair_created_at": "DATETIME",
    "discovery_source": "VARCHAR(128)",
    "last_discovered_at": "DATETIME",
    "filter_status": "VARCHAR(32)",
    "filter_reasons": "VARCHAR(256)",
}
_SNAPSHOT_COLUMNS: dict[str, str] = {
    "volume_5m": "FLOAT",
    "volume_1h": "FLOAT",
    "fdv": "FLOAT",
    "buys_5m": "INTEGER",
    "sells_5m": "INTEGER",
    "buys_1h": "INTEGER",
    "sells_1h": "INTEGER",
    "buys_24h": "INTEGER",
    "sells_24h": "INTEGER",
    "dex_name": "VARCHAR(64)",
    "pair_address": "VARCHAR(88)",
    "pair_created_at": "DATETIME",
    "active_boosts": "INTEGER",
    "discovery_source": "VARCHAR(128)",
    "filter_status": "VARCHAR(32)",
    "filter_reasons": "VARCHAR(256)",
}
_NARRATIVE_COLUMNS: dict[str, str] = {
    "keywords": "TEXT",
    "mention_count": "INTEGER",
    "mention_growth": "FLOAT",
    "engagement_growth": "FLOAT",
    "independent_sources": "INTEGER",
    "independent_authors": "INTEGER",
    "recency_score": "FLOAT",
    "acceleration": "FLOAT",
    "persistence": "FLOAT",
    "spam_score": "FLOAT",
    "confidence": "FLOAT",
    "data_completeness": "FLOAT",
    "explanation": "TEXT",
}
_NEWS_COLUMNS: dict[str, str] = {
    "category": "VARCHAR(64)",
    "normalized_title": "VARCHAR(512)",
    "feed_url": "VARCHAR(1024)",
    "observed_at": "DATETIME",
    "recorded_at": "DATETIME",
}
_RISK_ASSESSMENT_COLUMNS: dict[str, str] = {
    "observed_risk": "FLOAT",
    "observed_risk_band": "VARCHAR(32)",
    "source": "VARCHAR(64)",
    "observed_at": "DATETIME",
    "recorded_at": "DATETIME",
    "slot": "INTEGER",
}
_SECURITY_SCAN_COLUMNS: dict[str, str] = {
    "source": "VARCHAR(64)",
    "observed_at": "DATETIME",
    "recorded_at": "DATETIME",
    "slot": "INTEGER",
    "metadata_status": "VARCHAR(32)",
    "metadata_account": "VARCHAR(88)",
    "metadata_update_authority": "VARCHAR(88)",
    "metadata_name": "VARCHAR(128)",
    "metadata_symbol": "VARCHAR(32)",
    "metadata_uri": "VARCHAR(512)",
    "metadata_is_mutable": "INTEGER",
}
_RISK_HISTORY_COLUMNS: dict[str, str] = {
    "observed_risk": "FLOAT",
    "observed_risk_band": "VARCHAR(32)",
}
_CREATOR_OBSERVATION_COLUMNS: dict[str, str] = {
    "attempted_at": "DATETIME",
}
_TOKEN_ACCOUNT_OWNER_COLUMNS: dict[str, str] = {
    "attempted_at": "DATETIME",
}
_METAPLEX_CREATOR_COLUMNS: dict[str, str] = {
    "attempted_at": "DATETIME",
}
_PAPER_ORDER_COLUMNS: dict[str, str] = {
    "paper_quote_id": "INTEGER",
}
_PAPER_LOCK_COLUMNS: dict[str, str] = {
    "cycle_kind": "VARCHAR(16)",
}
_OPPORTUNITY_COLUMNS: dict[str, str] = {
    "snapshot_count": "INTEGER",
    "last_snapshot_at": "DATETIME",
    "snapshot_age_hours": "FLOAT",
    "narrative_match_count": "INTEGER",
    "security_coverage": "VARCHAR(32)",
}
_SAFE_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_tokens_discovery_source", "tokens", "discovery_source"),
    ("ix_tokens_filter_status", "tokens", "filter_status"),
    ("ix_tokens_pair_created_at", "tokens", "pair_created_at"),
    ("ix_news_items_normalized_title", "news_items", "normalized_title"),
    ("ix_news_items_category", "news_items", "category"),
)


def _add_missing_columns(table: str, columns: dict[str, str]) -> None:
    inspector = inspect(get_engine())
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    with get_engine().begin() as connection:
        for name, sql_type in columns.items():
            if name in existing:
                continue
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
            logger.info("Added column %s.%s", table, name)


def migrate_schema() -> None:
    """Create new tables and add missing columns. Does not drop data."""
    from app import models  # noqa: F401
    from app.data_protection import install_append_only_guards

    Base.metadata.create_all(bind=get_engine())
    _add_missing_columns("tokens", _TOKEN_COLUMNS)
    _add_missing_columns("market_snapshots", _SNAPSHOT_COLUMNS)
    _add_missing_columns("narratives", _NARRATIVE_COLUMNS)
    _add_missing_columns("news_items", _NEWS_COLUMNS)
    _add_missing_columns("token_security_scans", _SECURITY_SCAN_COLUMNS)
    _add_missing_columns("token_risk_assessments", _RISK_ASSESSMENT_COLUMNS)
    _add_missing_columns("risk_score_history", _RISK_HISTORY_COLUMNS)
    _add_missing_columns("opportunity_assessments", _OPPORTUNITY_COLUMNS)
    _add_missing_columns("creator_observations", _CREATOR_OBSERVATION_COLUMNS)
    _add_missing_columns("token_account_owner_observations", _TOKEN_ACCOUNT_OWNER_COLUMNS)
    _add_missing_columns("metaplex_creator_observations", _METAPLEX_CREATOR_COLUMNS)
    _add_missing_columns("paper_orders", _PAPER_ORDER_COLUMNS)
    _add_missing_columns("paper_auto_entry_locks", _PAPER_LOCK_COLUMNS)
    with get_engine().begin() as connection:
        for index_name, table, column in _SAFE_INDEXES:
            connection.execute(
                text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})")
            )
    logger.info("Database schema migrated")
    settings = get_settings()
    install_append_only_guards(get_engine(), settings.database_url)


def init_db() -> None:
    """Create tables and apply additive schema updates."""
    from app.data_protection import is_protected_catalog
    from app.services.sqlite_backup import backup_configured_database, persist_backup_event

    settings = get_settings()
    pre_backup = None
    if is_protected_catalog(settings.database_url) and settings.sqlite_backup_on_startup:
        pre_backup = backup_configured_database(settings.database_url, settings)
    migrate_schema()
    if pre_backup is not None and pre_backup.status != "skipped":
        session = get_session_factory()()
        try:
            persist_backup_event(session, pre_backup)
        except Exception:
            logger.exception("Failed to persist sqlite backup event")
        finally:
            session.close()
        if pre_backup.status == "failed":
            logger.error("Startup SQLite backup failed error=%s", pre_backup.error)
    logger.info("Database schema initialized")


def check_database_connection(session: Session | None = None) -> bool:
    """Return True when a simple SELECT 1 succeeds."""
    try:
        if session is not None:
            session.execute(text("SELECT 1"))
            return True
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database connectivity check failed")
        return False
