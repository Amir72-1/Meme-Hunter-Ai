"""Guards that keep production market history append-only.

Test databases remain writable so fixtures can isolate rows. Production
`meme_hunter.db` cannot be unlinked, dropped, or row-deleted without an
explicit confirmation phrase.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy.engine import Engine, make_url
from sqlalchemy.engine.url import URL

logger = logging.getLogger("meme_hunter.data_protection")

DESTRUCTIVE_CONFIRMATION = "CONFIRM_DELETE_HISTORICAL_MARKET_DATA"
PRODUCTION_SQLITE_NAME = "meme_hunter.db"
TEST_SQLITE_NAME = "test_meme_hunter.db"

PROTECTED_DELETE_TABLES = (
    "tokens",
    "market_snapshots",
    "discovery_scans",
    "historical_evaluations",
    "quality_historical_evaluations",
    "quality_historical_outcomes",
    "point_in_time_runs",
    "point_in_time_candidates",
    "point_in_time_outcomes",
)
APPEND_ONLY_UPDATE_TABLES = ("market_snapshots",)


class DestructiveOperationBlocked(RuntimeError):
    """Raised when a destructive catalog operation is refused."""


def sqlite_file_from_url(database_url: str, *, cwd: Path | None = None) -> Path | None:
    url = make_url(database_url)
    if url.drivername != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    path = Path(database)
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    return path.resolve()


def is_ephemeral_test_database(database_url: str, *, cwd: Path | None = None) -> bool:
    url = make_url(database_url)
    if url.drivername != "sqlite":
        return False
    if not url.database or url.database == ":memory:":
        return True
    path = sqlite_file_from_url(database_url, cwd=cwd)
    if path is None:
        return True
    name = path.name.lower()
    return name == TEST_SQLITE_NAME or name.startswith("test_") or "test_meme_hunter" in name


def is_protected_catalog(database_url: str, *, cwd: Path | None = None) -> bool:
    path = sqlite_file_from_url(database_url, cwd=cwd)
    if path is None:
        return False
    return path.name.lower() == PRODUCTION_SQLITE_NAME


def database_identity(database_url: str, *, cwd: Path | None = None) -> dict[str, object]:
    path = sqlite_file_from_url(database_url, cwd=cwd)
    exists = bool(path and path.exists())
    size = int(path.stat().st_size) if exists and path is not None else None
    mtime = None
    if exists and path is not None:
        mtime = path.stat().st_mtime
    return {
        "database_url_scheme": make_url(database_url).drivername,
        "resolved_path": str(path) if path is not None else None,
        "filename": path.name if path is not None else None,
        "exists": exists,
        "size_bytes": size,
        "mtime_epoch": mtime,
        "protected_catalog": is_protected_catalog(database_url, cwd=cwd),
        "ephemeral_test_database": is_ephemeral_test_database(database_url, cwd=cwd),
        "note": "Relative sqlite URLs resolve against the process working directory.",
    }


def require_destructive_confirmation(confirmation: str | None) -> None:
    if confirmation != DESTRUCTIVE_CONFIRMATION:
        raise DestructiveOperationBlocked(
            "DESTRUCTIVE_OP_BLOCKED: historical market data cannot be deleted or reset "
            f"without confirmation phrase {DESTRUCTIVE_CONFIRMATION}."
        )


def assert_safe_test_database_path(path: Path) -> None:
    resolved = path.resolve()
    if resolved.name.lower() == PRODUCTION_SQLITE_NAME:
        raise DestructiveOperationBlocked(
            "DESTRUCTIVE_OP_BLOCKED: refusing to reset production catalog "
            f"{resolved}. Tests must use {TEST_SQLITE_NAME}."
        )
    if not is_ephemeral_test_database(f"sqlite:///{resolved.as_posix()}"):
        raise DestructiveOperationBlocked(
            f"DESTRUCTIVE_OP_BLOCKED: refusing to unlink non-test database {resolved}."
        )


def assert_engine_reset_allowed(database_url: str) -> None:
    env = os.environ.get("APP_ENV", "").lower()
    if env == "test" and is_protected_catalog(database_url):
        raise DestructiveOperationBlocked(
            "DESTRUCTIVE_OP_BLOCKED: test processes cannot attach the production "
            f"catalog ({PRODUCTION_SQLITE_NAME})."
        )


def _trigger_name(kind: str, table: str) -> str:
    return f"protect_{kind}_{table}"


def append_only_triggers_sql() -> list[str]:
    statements: list[str] = []
    for table in PROTECTED_DELETE_TABLES:
        name = _trigger_name("delete", table)
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT, "
            f"'DESTRUCTIVE_OP_BLOCKED: {table} rows are preserved'); END;"
        )
    for table in APPEND_ONLY_UPDATE_TABLES:
        name = _trigger_name("update", table)
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT, "
            f"'DESTRUCTIVE_OP_BLOCKED: {table} is append-only'); END;"
        )
    return statements


def install_append_only_guards(engine: Engine, database_url: str) -> bool:
    if not is_protected_catalog(database_url):
        return False
    statements = append_only_triggers_sql()
    with engine.begin() as connection:
        for sql in statements:
            connection.exec_driver_sql(sql)
    logger.info("Installed append-only guards on protected catalog")
    return True


def remove_append_only_guards(engine: Engine, database_url: str, confirmation: str) -> None:
    require_destructive_confirmation(confirmation)
    if not is_protected_catalog(database_url):
        raise DestructiveOperationBlocked(
            "DESTRUCTIVE_OP_BLOCKED: append-only guards are only defined on the production catalog."
        )
    names = [_trigger_name("delete", table) for table in PROTECTED_DELETE_TABLES]
    names.extend(_trigger_name("update", table) for table in APPEND_ONLY_UPDATE_TABLES)
    with engine.begin() as connection:
        for name in names:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    logger.warning("Removed append-only guards after explicit confirmation")


def url_database_name(url: str | URL) -> str | None:
    parsed = url if isinstance(url, URL) else make_url(url)
    return parsed.database
