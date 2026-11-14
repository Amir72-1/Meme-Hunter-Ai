"""In-process DexScreener discovery scheduler. Single asyncio task per process."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.services.dexscreener_client import DexScreenerClient
from app.services.token_discovery import DiscoveryScanResult, run_discovery_scan
from app.timeutil import utc_now

logger = logging.getLogger("meme_hunter.scheduler")

SKIPPED = "skipped"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"
NEVER = "never"

_SECRETISH = re.compile(
    r"(?i)(bearer\s+)\S+|(?:api[_-]?key|secret|token|password)\s*[:=]\s*\S+"
)


def sanitize_error(exc: BaseException | str) -> str:
    text = str(exc)
    text = _SECRETISH.sub("[redacted]", text)
    return text[:500]


def skipped_scan_result() -> DiscoveryScanResult:
    now = utc_now()
    return DiscoveryScanResult(status=SKIPPED, started_at=now, finished_at=now)


class DiscoveryScheduler:
    """Coordinates manual and automatic scans. Not safe across multiple OS processes."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self.enabled = False
        self.interval_minutes = 15.0
        self.scan_in_progress = False
        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_status = NEVER
        self.last_error: str | None = None
        self.successful_runs = 0
        self.failed_runs = 0
        self.skipped_runs = 0
        self.next_run_at: datetime | None = None
        self.catchup_pending = False
        self.recovered_abandoned_scan_ids: list[int] = []
        self._first_wait_seconds: float | None = None
        self._wait = self._sleep_or_stop
        self._scan = self._run_discovery

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def is_busy(self) -> bool:
        return self.scan_in_progress or (self._lock is not None and self._lock.locked())

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status_payload(self, session: Session | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": self.enabled,
            "running": self.running,
            "scan_in_progress": self.is_busy(),
            "interval_minutes": self.interval_minutes,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "skipped_runs": self.skipped_runs,
            "next_run_at": self.next_run_at,
            "catchup_pending": self.catchup_pending,
            "recovered_abandoned_scan_ids": list(self.recovered_abandoned_scan_ids),
            "completed_scans_total": self.successful_runs,
            "failed_scans_total": self.failed_runs,
            "interrupted_scans_total": 0,
            "last_successful_scan_id": None,
            "last_successful_scan_at": None,
            "last_failed_scan_id": None,
            "last_failed_scan_at": None,
            "last_scan_duration_seconds": None,
            "consecutive_scan_failures": 0,
            "database_identity": None,
            "last_successful_backup": None,
            "note": (
                "Automatic collection runs only while this backend process is up. "
                "Closing the terminal or stopping uvicorn stops the scheduler. "
                "Overlap prevention is single-process only; do not run multiple uvicorn workers. "
                "Historical completed/failed counts come from discovery_scans after restart. "
                "Do not invent snapshots for offline gaps. Do not use multiple uvicorn workers."
            ),
        }
        if session is not None:
            from app.data_protection import database_identity
            from app.services.discovery_recovery import persistent_scheduler_stats
            from app.services.sqlite_backup import last_successful_backup

            hist = persistent_scheduler_stats(session)
            payload["database_identity"] = database_identity(get_settings().database_url)
            payload["last_successful_backup"] = last_successful_backup(session)
            payload["successful_runs"] = hist["completed_scans_total"]
            payload["failed_runs"] = hist["failed_scans_total"]
            payload["completed_scans_total"] = hist["completed_scans_total"]
            payload["failed_scans_total"] = hist["failed_scans_total"]
            payload["interrupted_scans_total"] = hist["interrupted_scans_total"]
            payload["last_successful_scan_id"] = hist["last_successful_scan_id"]
            payload["last_successful_scan_at"] = hist["last_successful_scan_at"]
            payload["last_failed_scan_id"] = hist["last_failed_scan_id"]
            payload["last_failed_scan_at"] = hist["last_failed_scan_at"]
            payload["last_scan_duration_seconds"] = hist["last_scan_duration_seconds"]
            payload["consecutive_scan_failures"] = hist["consecutive_scan_failures"]
            if hist["last_scan_status"] != NEVER:
                payload["last_status"] = hist["last_scan_status"]
            if hist["persistent_last_started_at"] and self.last_started_at is None:
                payload["last_started_at"] = hist["persistent_last_started_at"]
            if hist["persistent_last_finished_at"] and self.last_finished_at is None:
                payload["last_finished_at"] = hist["persistent_last_finished_at"]
        return payload

    def record_skip(self) -> None:
        self.skipped_runs += 1
        self.last_status = SKIPPED
        self.last_finished_at = utc_now()
        logger.info("Discovery scan skipped because another scan is already running")

    def record_result(self, result: DiscoveryScanResult) -> None:
        self.last_finished_at = result.finished_at or utc_now()
        if result.status in {FAILED, INTERRUPTED}:
            self.failed_runs += 1
            self.last_status = result.status
            if result.errors:
                self.last_error = sanitize_error(result.errors[0].detail)
            else:
                self.last_error = "Discovery scan failed"
            return
        if result.status == SKIPPED:
            return
        self.successful_runs += 1
        self.last_status = result.status or COMPLETED
        self.last_error = None

    def record_exception(self, exc: BaseException) -> None:
        self.failed_runs += 1
        self.last_status = FAILED
        self.last_error = sanitize_error(exc)
        self.last_finished_at = utc_now()

    async def try_execute(
        self,
        session: Session,
        settings: Settings,
        client: DexScreenerClient,
    ) -> DiscoveryScanResult:
        lock = self._ensure_lock()
        if self.is_busy():
            self.record_skip()
            return skipped_scan_result()
        async with lock:
            self.scan_in_progress = True
            self.last_started_at = utc_now()
            try:
                result = await self._scan(session, client, settings)
                self.record_result(result)
                return result
            except asyncio.CancelledError as exc:
                self.record_exception(exc)
                self.last_status = INTERRUPTED
                raise
            except Exception as exc:
                self.record_exception(exc)
                raise
            finally:
                self.scan_in_progress = False
                if self.last_finished_at is None:
                    self.last_finished_at = utc_now()

    async def start(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self.enabled = bool(settings.auto_discovery_enabled)
        self.interval_minutes = float(settings.auto_discovery_interval_minutes)
        if self._task is not None and not self._task.done():
            logger.info("Automatic discovery scheduler already running; not starting a second task")
            return
        self._reset_runtime_stats()
        self._prepare_startup(settings)
        if not self.enabled:
            self.next_run_at = None
            self.catchup_pending = False
            logger.info("Automatic discovery is disabled")
            return
        self._stop = asyncio.Event()
        wait = self._first_wait_seconds or settings.auto_discovery_interval_seconds
        self.next_run_at = utc_now() + timedelta(seconds=wait)
        self._task = asyncio.create_task(self._loop(settings), name="auto-discovery")
        logger.info(
            "Automatic discovery scheduler started interval_minutes=%s first_run_at=%s catchup=%s",
            self.interval_minutes,
            self.next_run_at.isoformat(),
            self.catchup_pending,
        )

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.next_run_at = None
        logger.info("Automatic discovery scheduler stopped")

    def _reset_runtime_stats(self) -> None:
        self.successful_runs = 0
        self.failed_runs = 0
        self.skipped_runs = 0
        self.last_started_at = None
        self.last_finished_at = None
        self.last_status = NEVER
        self.last_error = None
        self.catchup_pending = False
        self.recovered_abandoned_scan_ids = []
        self._first_wait_seconds = None

    def _prepare_startup(self, settings: Settings) -> None:
        """Recover orphan running rows and decide a single overdue catch-up tick."""
        from app.services.discovery_recovery import last_completed_scan, recover_abandoned_scans, scan_is_overdue

        self._first_wait_seconds = settings.auto_discovery_interval_seconds
        self.catchup_pending = False
        session = get_session_factory()()
        try:
            self.recovered_abandoned_scan_ids = recover_abandoned_scans(session)
            last = last_completed_scan(session)
            if scan_is_overdue(last, settings):
                self.catchup_pending = True
                self._first_wait_seconds = min(
                    float(settings.discovery_startup_catchup_delay_seconds),
                    settings.auto_discovery_interval_seconds,
                )
                logger.info(
                    "Overdue collection after last completed scan id=%s; one catch-up tick in %ss. No backfill.",
                    last.id if last else None,
                    self._first_wait_seconds,
                )
            else:
                logger.info(
                    "First-start policy: wait one full interval before the first scan "
                    "(no prior completed scan, or last scan is still within interval)."
                )
        except Exception:
            logger.exception("Startup recovery failed; scheduler will still start")
            self._first_wait_seconds = settings.auto_discovery_interval_seconds
        finally:
            session.close()

    async def _sleep_or_stop(self, seconds: float) -> bool:
        if self._stop is None:
            self._stop = asyncio.Event()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _loop(self, settings: Settings) -> None:
        first_wait = True
        try:
            while self._stop is None or not self._stop.is_set():
                if first_wait:
                    interval = self._first_wait_seconds or settings.auto_discovery_interval_seconds
                else:
                    interval = settings.auto_discovery_interval_seconds
                self.next_run_at = utc_now() + timedelta(seconds=interval)
                stopped = await self._wait(interval)
                if stopped:
                    return
                first_wait = False
                self.catchup_pending = False
                try:
                    await self._scheduled_tick(settings)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Scheduled discovery loop recovered after an unexpected error")
                    self.record_exception(exc)
        except asyncio.CancelledError:
            logger.info("Automatic discovery task cancelled")
            raise

    async def _scheduled_tick(self, settings: Settings) -> None:
        if self.is_busy():
            self.record_skip()
            return
        session = get_session_factory()()
        try:
            async with DexScreenerClient(settings=settings) as client:
                result = await self.try_execute(session, settings, client)
            logger.info(
                "Scheduled discovery finished status=%s snapshots=%s",
                result.status,
                result.snapshots_saved,
            )
        except Exception:
            logger.exception("Scheduled discovery tick failed; continuing")
        finally:
            session.close()

    async def _run_discovery(
        self,
        session: Session,
        client: DexScreenerClient,
        settings: Settings,
    ) -> DiscoveryScanResult:
        return await run_discovery_scan(session, client, settings)


_scheduler: DiscoveryScheduler | None = None


def get_scheduler() -> DiscoveryScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DiscoveryScheduler()
    return _scheduler


def reset_scheduler() -> DiscoveryScheduler:
    """Replace the process singleton. Used by tests."""

    global _scheduler
    _scheduler = DiscoveryScheduler()
    return _scheduler
