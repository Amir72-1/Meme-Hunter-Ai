"""Test fixtures. Uses a dedicated SQLite file, never meme_hunter.db."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

TEST_DB_PATH = Path(__file__).resolve().parent.parent / "test_meme_hunter.db"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["PAPER_TRADING_ENABLED"] = "true"
os.environ["PAPER_AUTO_TRADING"] = "false"
os.environ["PAPER_AUTO_ENTRY_ENABLED"] = "false"
os.environ["PAPER_AUTO_EXIT_ENABLED"] = "false"
os.environ["PAPER_STARTING_BALANCE"] = "100"
os.environ["SECURITY_MIN_COMPLETENESS"] = "0.50"
os.environ["SECURITY_MIN_CONFIDENCE"] = "0.50"
os.environ["AUTO_DISCOVERY_ENABLED"] = "false"
os.environ["SQLITE_BACKUP_ENABLED"] = "false"
os.environ["SQLITE_BACKUP_ON_STARTUP"] = "false"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.database import init_db, reset_engine  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Generator[None, None, None]:
    from app.data_protection import assert_safe_test_database_path

    assert_safe_test_database_path(TEST_DB_PATH)
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    reset_engine(os.environ["DATABASE_URL"])
    init_db()
    yield
    reset_engine(os.environ["DATABASE_URL"]).dispose()
    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink(missing_ok=True)
        except PermissionError:
            pass


@pytest.fixture(autouse=True)
def _reset_rpc_session_and_cooldown() -> Generator[None, None, None]:
    from app.services.paper_entry_scheduler import reset_entry_scheduler
    from app.services.paper_exit_scheduler import reset_exit_scheduler
    from app.services.rpc_cooldown import clear_rpc_cooldown, reset_cooldown_store
    from app.services.rpc_session import reset_shared_rpc_session

    reset_cooldown_store()
    clear_rpc_cooldown()
    reset_shared_rpc_session()
    reset_entry_scheduler()
    reset_exit_scheduler()
    yield
    reset_shared_rpc_session()
    clear_rpc_cooldown()
    reset_entry_scheduler()
    reset_exit_scheduler()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client
