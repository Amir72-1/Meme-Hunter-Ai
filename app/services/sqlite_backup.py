"""Online SQLite backup. Never restores or overwrites the live catalog."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.data_protection import (
    database_identity,
    is_ephemeral_test_database,
    is_protected_catalog,
    sqlite_file_from_url,
)
from app.timeutil import utc_now

logger = logging.getLogger("meme_hunter.backup")

_SECRETISH = re.compile(
    r"(?i)(bearer\s+)\S+|(?:api[_-]?key|secret|token|password)\s*[:=]\s*\S+"
)


def sanitize_backup_error(exc: BaseException | str) -> str:
    text = str(exc)
    text = _SECRETISH.sub("[redacted]", text)
    return text[:500]


@dataclass
class BackupResult:
    status: str
    source_path: str | None
    backup_path: str | None
    started_at: datetime
    finished_at: datetime | None
    bytes: int | None
    integrity: str | None
    error: str | None
    skipped_reason: str | None = None

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "source_path": self.source_path,
            "backup_path": self.backup_path,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "bytes": self.bytes,
            "integrity": self.integrity,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
            "restore": False,
            "note": "Backup only. This process never restores or overwrites the live database.",
        }


def backup_directory(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = Path(settings.sqlite_backup_dir)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _timestamp_name(now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"meme_hunter_{stamp}.db"


def verify_sqlite_integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "missing"
    finally:
        connection.close()


def copy_sqlite_online(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(destination), timeout=30)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def prune_backup_files(directory: Path, retain: int, *, live_path: Path | None) -> list[str]:
    retain = max(int(retain), 1)
    files = sorted(
        [path for path in directory.glob("meme_hunter_*.db") if path.is_file()],
        key=lambda item: (item.name, item.stat().st_mtime),
        reverse=True,
    )
    removed: list[str] = []
    kept = 0
    for path in files:
        if live_path is not None and path.resolve() == live_path.resolve():
            logger.error("Refusing to prune live catalog path %s", path)
            continue
        kept += 1
        if kept <= retain:
            continue
        path.unlink()
        removed.append(str(path))
    return removed


def _newest_backup_mtime(directory: Path) -> float | None:
    files = [path for path in directory.glob("meme_hunter_*.db") if path.is_file()]
    if not files:
        return None
    return max(path.stat().st_mtime for path in files)


def should_skip_backup(database_url: str, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    if not settings.sqlite_backup_enabled:
        return "backups_disabled"
    if is_ephemeral_test_database(database_url):
        return "ephemeral_test_database"
    path = sqlite_file_from_url(database_url)
    if path is None:
        return "not_sqlite_file"
    if not path.exists():
        return "source_missing"
    interval = float(settings.sqlite_backup_min_interval_seconds)
    if interval > 0:
        newest = _newest_backup_mtime(backup_directory(settings))
        if newest is not None:
            if time.time() - newest < interval:
                return "recent_backup"
    return None


def backup_configured_database(
    database_url: str | None = None,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> BackupResult:
    settings = settings or get_settings()
    url = database_url or settings.database_url
    started = now or utc_now()
    identity = database_identity(url)
    source = sqlite_file_from_url(url)
    skip = should_skip_backup(url, settings)
    if skip:
        logger.info("SQLite backup skipped reason=%s", skip)
        return BackupResult(
            status="skipped",
            source_path=str(source) if source else None,
            backup_path=None,
            started_at=started,
            finished_at=utc_now(),
            bytes=None,
            integrity=None,
            error=None,
            skipped_reason=skip,
        )

    assert source is not None
    dest = backup_directory(settings) / _timestamp_name(started)
    try:
        copy_sqlite_online(source, dest)
        integrity = verify_sqlite_integrity(dest)
        if integrity != "ok":
            raise RuntimeError(f"backup integrity_check={integrity}")
        finished = utc_now()
        prune_backup_files(dest.parent, settings.sqlite_backup_retain, live_path=source)
        logger.info(
            "SQLite backup completed path=%s bytes=%s integrity=%s protected=%s",
            dest,
            dest.stat().st_size,
            integrity,
            identity.get("protected_catalog"),
        )
        return BackupResult(
            status="completed",
            source_path=str(source),
            backup_path=str(dest),
            started_at=started,
            finished_at=finished,
            bytes=int(dest.stat().st_size),
            integrity=integrity,
            error=None,
        )
    except Exception as exc:
        finished = utc_now()
        error = sanitize_backup_error(exc)
        logger.error("SQLite backup failed error=%s", error)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                logger.exception("Failed to remove incomplete backup file")
        return BackupResult(
            status="failed",
            source_path=str(source),
            backup_path=None,
            started_at=started,
            finished_at=finished,
            bytes=None,
            integrity=None,
            error=error,
        )


def persist_backup_event(session: Session, result: BackupResult) -> None:
    from app.models import SqliteBackupEvent

    row = SqliteBackupEvent(
        started_at=result.started_at,
        finished_at=result.finished_at,
        status=result.status,
        source_path=result.source_path,
        backup_path=result.backup_path,
        size_bytes=result.bytes,
        integrity=result.integrity,
        skipped_reason=result.skipped_reason,
        error=result.error,
    )
    session.add(row)
    session.commit()


def last_successful_backup(session: Session) -> dict[str, object] | None:
    from app.models import SqliteBackupEvent

    row = session.scalar(
        select(SqliteBackupEvent)
        .where(SqliteBackupEvent.status == "completed")
        .order_by(SqliteBackupEvent.finished_at.desc(), SqliteBackupEvent.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    finished = row.finished_at.isoformat() if row.finished_at is not None else None
    return {
        "id": row.id,
        "status": row.status,
        "backup_path": row.backup_path,
        "source_path": row.source_path,
        "finished_at": finished,
        "size_bytes": row.size_bytes,
        "integrity": row.integrity,
    }


def latest_backup_status(session: Session) -> dict[str, object] | None:
    from app.models import SqliteBackupEvent

    row = session.scalar(select(SqliteBackupEvent).order_by(SqliteBackupEvent.id.desc()).limit(1))
    if row is None:
        return None
    return {
        "id": row.id,
        "status": row.status,
        "backup_path": row.backup_path,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "error": row.error,
        "skipped_reason": row.skipped_reason,
        "integrity": row.integrity,
    }
