"""Optional in-process paper auto-entry scheduler. Disabled by default. No catch-up trades."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.services.paper_auto_entry import (
    STATUS_SKIPPED,
    TRIGGER_SCHEDULED,
    run_auto_entry_cycle,
)
from app.timeutil import utc_now

logger = logging.getLogger("meme_hunter.paper_entry")

NEVER = "never"


class PaperEntryScheduler:
    """One asyncio task per process. Database lease fails closed if another cycle is running."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self.enabled = False
        self.interval_minutes = 5.0
        self.cycle_in_progress = False
        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_status = NEVER
        self.last_error: str | None = None
        self.successful_runs = 0
        self.failed_runs = 0
        self.skipped_runs = 0
        self.next_run_at: datetime | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_busy(self) -> bool:
        return self.cycle_in_progress or (self._lock is not None and self._lock.locked())

    def status_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "cycle_in_progress": self.is_busy(),
            "interval_minutes": self.interval_minutes,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "skipped_runs": self.skipped_runs,
            "next_run_at": self.next_run_at,
            "catchup_pending": False,
            "automatic_selling": False,
            "live_trading": False,
            "wallet_connected": False,
            "dexscreener_refresh": False,
            "note": (
                "Paper auto-entry is disabled by default. When enabled, the in-process scheduler "
                "waits one full interval after start (no catch-up trades). Cycles use stored "
                "evidence only and submit hypothetical paper buys. Overlap is single-process "
                "plus a SQLite lease; do not run multiple uvicorn workers. Not market execution."
            ),
        }

    async def start(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self.enabled = bool(settings.paper_auto_entry_enabled)
        self.interval_minutes = float(settings.paper_auto_entry_interval_minutes)
        if self._task is not None and not self._task.done():
            logger.info("Paper auto-entry scheduler already running; not starting a second task")
            return
        self.next_run_at = None
        if not self.enabled:
            logger.info("Paper auto-entry scheduler is disabled")
            return
        self._stop = asyncio.Event()
        wait = settings.paper_auto_entry_interval_seconds
        self.next_run_at = utc_now() + timedelta(seconds=wait)
        self._task = asyncio.create_task(self._loop(settings), name="paper-auto-entry")
        logger.info(
            "Paper auto-entry scheduler started interval_minutes=%s first_run_at=%s (no catch-up)",
            self.interval_minutes,
            self.next_run_at.isoformat(),
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
        logger.info("Paper auto-entry scheduler stopped")

    async def _sleep_or_stop(self, seconds: float) -> bool:
        if self._stop is None:
            self._stop = asyncio.Event()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _loop(self, settings: Settings) -> None:
        try:
            while self._stop is None or not self._stop.is_set():
                interval = settings.paper_auto_entry_interval_seconds
                self.next_run_at = utc_now() + timedelta(seconds=interval)
                stopped = await self._sleep_or_stop(interval)
                if stopped:
                    return
                try:
                    await self.run_scheduled(settings)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Paper auto-entry loop recovered after an unexpected error")
                    self.failed_runs += 1
                    self.last_status = "failed"
                    self.last_error = str(exc)[:300]
                    self.last_finished_at = utc_now()
        except asyncio.CancelledError:
            logger.info("Paper auto-entry task cancelled")
            raise

    async def run_scheduled(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        lock = self._ensure_lock()
        if lock.locked() or self.cycle_in_progress:
            self.skipped_runs += 1
            self.last_status = STATUS_SKIPPED
            logger.info("Scheduled paper auto-entry skipped because a cycle is already running")
            return
        async with lock:
            self.cycle_in_progress = True
            self.last_started_at = utc_now()
            session = get_session_factory()()
            try:
                summary = run_auto_entry_cycle(session, settings, trigger=TRIGGER_SCHEDULED)
                self.last_finished_at = summary.finished_at or utc_now()
                self.last_status = summary.status
                if summary.status == STATUS_SKIPPED:
                    self.skipped_runs += 1
                    self.last_error = None
                elif summary.status == "failed":
                    self.failed_runs += 1
                    self.last_error = summary.errors[0]["detail"] if summary.errors else "cycle failed"
                else:
                    self.successful_runs += 1
                    self.last_error = None
                    if summary.status == "completed_with_reporting_error":
                        self.last_error = "Reporting failed after durable paper fills; book was not rolled back."
            except asyncio.CancelledError:
                self.last_status = "interrupted"
                self.last_finished_at = utc_now()
                raise
            except Exception as exc:
                self.failed_runs += 1
                self.last_status = "failed"
                self.last_error = str(exc)[:300]
                self.last_finished_at = utc_now()
                logger.exception("Scheduled paper auto-entry cycle failed")
            finally:
                session.close()
                self.cycle_in_progress = False


_scheduler: PaperEntryScheduler | None = None


def get_entry_scheduler() -> PaperEntryScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = PaperEntryScheduler()
    return _scheduler


def reset_entry_scheduler() -> PaperEntryScheduler:
    global _scheduler
    _scheduler = PaperEntryScheduler()
    return _scheduler
