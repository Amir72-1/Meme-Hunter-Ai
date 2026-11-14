"""Persistent Solana RPC cooldown. Survives HTTP requests and process restarts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.models import RpcCooldownState, utc_now
from app.services.rpc_accounting import KIND_COOLDOWN_ACTIVE, KIND_RATE_LIMITED
from app.timeutil import as_utc

RPC_COOLDOWN_SCOPE = "solana_rpc"


@dataclass
class RpcCooldownSnapshot:
    active: bool
    scope: str = RPC_COOLDOWN_SCOPE
    reason: str | None = None
    until_at: datetime | None = None
    retry_after_seconds: float | None = None
    backoff_seconds: float | None = None
    consecutive_429: int = 0
    activated_at: datetime | None = None
    cleared_at: datetime | None = None
    expired: bool = False
    last_until_at: datetime | None = None
    last_reason: str | None = None
    blocking: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "scope": self.scope,
            "reason": self.reason,
            "status": KIND_COOLDOWN_ACTIVE if self.active else None,
            "until_at": self.until_at.isoformat() if self.until_at else None,
            "retry_after_seconds": self.retry_after_seconds,
            "backoff_seconds": self.backoff_seconds,
            "consecutive_429": self.consecutive_429,
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "cleared_at": self.cleared_at.isoformat() if self.cleared_at else None,
            "expired": self.expired,
            "last_until_at": self.last_until_at.isoformat() if self.last_until_at else None,
            "last_reason": self.last_reason,
            "blocking": self.blocking,
        }


def compute_cooldown_seconds(
    settings: Settings | None,
    retry_after: float | None,
    consecutive_429: int,
) -> float:
    settings = settings or get_settings()
    cap = max(0.0, float(settings.solana_rpc_cooldown_max_seconds))
    base = max(0.0, float(settings.solana_rpc_cooldown_base_seconds))
    if retry_after is not None:
        wait = max(0.0, float(retry_after))
        return min(wait, cap) if cap else wait
    exponent = max(int(consecutive_429) - 1, 0)
    wait = base * (2 ** exponent) if base else 0.0
    return min(wait, cap) if cap else wait


class RpcCooldownStore:
    def active(self, now: datetime | None = None) -> RpcCooldownSnapshot | None:
        raise NotImplementedError

    def snapshot(self, now: datetime | None = None) -> RpcCooldownSnapshot:
        raise NotImplementedError

    def activate(
        self,
        *,
        until_at: datetime,
        reason: str = KIND_RATE_LIMITED,
        retry_after_seconds: float | None,
        backoff_seconds: float,
        consecutive_429: int,
    ) -> RpcCooldownSnapshot:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class DatabaseRpcCooldownStore(RpcCooldownStore):
    def __init__(self, scope: str = RPC_COOLDOWN_SCOPE) -> None:
        self.scope = scope

    def _session(self) -> Session:
        return get_session_factory()()

    def _row(self, session: Session) -> RpcCooldownState | None:
        return session.get(RpcCooldownState, self.scope)

    def snapshot(self, now: datetime | None = None) -> RpcCooldownSnapshot:
        now = as_utc(now or utc_now())
        session = self._session()
        try:
            row = self._row(session)
            if row is None:
                return RpcCooldownSnapshot(active=False, scope=self.scope)
            until = as_utc(row.until_at) if row.until_at is not None else None
            active = until is not None and now is not None and until > now
            expired = bool(until is not None and now is not None and until <= now)
            if expired and row.cleared_at is None:
                row.cleared_at = now
                row.updated_at = now
                session.commit()
            return RpcCooldownSnapshot(
                active=active,
                scope=row.scope,
                reason=row.reason if active else None,
                until_at=until if active else None,
                retry_after_seconds=row.retry_after_seconds if active else None,
                backoff_seconds=row.backoff_seconds if active else None,
                consecutive_429=int(row.consecutive_429 or 0) if active else 0,
                activated_at=as_utc(row.activated_at) if active and row.activated_at else None,
                cleared_at=as_utc(row.cleared_at),
                expired=expired,
                last_until_at=until,
                last_reason=row.reason,
                blocking=active,
            )
        finally:
            session.close()

    def active(self, now: datetime | None = None) -> RpcCooldownSnapshot | None:
        snap = self.snapshot(now=now)
        return snap if snap.active else None

    def activate(
        self,
        *,
        until_at: datetime,
        reason: str = KIND_RATE_LIMITED,
        retry_after_seconds: float | None,
        backoff_seconds: float,
        consecutive_429: int,
    ) -> RpcCooldownSnapshot:
        session = self._session()
        try:
            row = self._row(session)
            stamp = utc_now()
            if row is None:
                row = RpcCooldownState(scope=self.scope)
                session.add(row)
            row.until_at = until_at
            row.reason = reason
            row.retry_after_seconds = retry_after_seconds
            row.backoff_seconds = backoff_seconds
            row.consecutive_429 = int(consecutive_429)
            row.activated_at = stamp
            row.cleared_at = None
            row.last_http_429_at = stamp
            row.updated_at = stamp
            session.commit()
            until = as_utc(until_at)
            active = until is not None and until > stamp
            return RpcCooldownSnapshot(
                active=active,
                scope=self.scope,
                reason=reason if active else None,
                until_at=until if active else None,
                retry_after_seconds=retry_after_seconds if active else None,
                backoff_seconds=backoff_seconds if active else None,
                consecutive_429=int(consecutive_429) if active else 0,
                activated_at=stamp if active else None,
                blocking=active,
                expired=bool(until is not None and not active),
                last_until_at=until,
                last_reason=reason,
            )
        finally:
            session.close()

    def clear(self) -> None:
        session = self._session()
        try:
            row = self._row(session)
            if row is None:
                return
            stamp = utc_now()
            until = as_utc(row.until_at)
            if until is None or until > stamp:
                row.until_at = stamp
            row.cleared_at = stamp
            row.updated_at = stamp
            session.commit()
        finally:
            session.close()


_store: RpcCooldownStore | None = None


def get_cooldown_store() -> RpcCooldownStore:
    global _store
    if _store is None:
        _store = DatabaseRpcCooldownStore()
    return _store


def set_cooldown_store(store: RpcCooldownStore | None) -> None:
    global _store
    _store = store


def reset_cooldown_store() -> RpcCooldownStore:
    store = DatabaseRpcCooldownStore()
    set_cooldown_store(store)
    return store


def clear_rpc_cooldown() -> None:
    get_cooldown_store().clear()


def cooldown_payload(now: datetime | None = None) -> dict[str, Any]:
    return get_cooldown_store().snapshot(now=now).as_dict()
