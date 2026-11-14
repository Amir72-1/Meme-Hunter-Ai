"""RPC attempt accounting and batch-stop helpers. Does not score tokens."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

KIND_RATE_LIMITED = "RATE_LIMITED"
KIND_COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
KIND_TIMEOUT = "TIMEOUT"
KIND_RETRYABLE = "RETRYABLE"
KIND_PROVIDER = "PROVIDER"
KIND_CLIENT = "CLIENT"

METHOD_FIELDS = ("attempts", "successes", "failures", "retries", "http_429", "timeouts")


@dataclass
class RpcAccounting:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    http_429: int = 0
    timeouts: int = 0
    other_errors: int = 0
    cache_hits: int = 0
    consecutive_429: int = 0
    stop_reason: str | None = None
    methods: dict[str, dict[str, int]] = field(default_factory=dict)

    def copy(self) -> RpcAccounting:
        return RpcAccounting(
            attempts=self.attempts,
            successes=self.successes,
            failures=self.failures,
            retries=self.retries,
            http_429=self.http_429,
            timeouts=self.timeouts,
            other_errors=self.other_errors,
            cache_hits=self.cache_hits,
            consecutive_429=self.consecutive_429,
            stop_reason=self.stop_reason,
            methods={name: dict(values) for name, values in self.methods.items()},
        )

    def delta(self, start: RpcAccounting) -> RpcAccounting:
        names = set(self.methods) | set(start.methods)
        methods: dict[str, dict[str, int]] = {}
        for name in names:
            current = self.methods.get(name, {})
            previous = start.methods.get(name, {})
            methods[name] = {
                field_name: int(current.get(field_name, 0)) - int(previous.get(field_name, 0))
                for field_name in METHOD_FIELDS
            }
        return RpcAccounting(
            attempts=self.attempts - start.attempts,
            successes=self.successes - start.successes,
            failures=self.failures - start.failures,
            retries=self.retries - start.retries,
            http_429=self.http_429 - start.http_429,
            timeouts=self.timeouts - start.timeouts,
            other_errors=self.other_errors - start.other_errors,
            cache_hits=self.cache_hits - start.cache_hits,
            consecutive_429=self.consecutive_429,
            stop_reason=self.stop_reason,
            methods=methods,
        )

    def bump_method(self, method: str, field_name: str, amount: int = 1) -> None:
        row = self.methods.setdefault(method, {name: 0 for name in METHOD_FIELDS})
        row[field_name] = int(row.get(field_name, 0)) + amount

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "http_429": self.http_429,
            "timeouts": self.timeouts,
            "other_errors": self.other_errors,
            "cache_hits": self.cache_hits,
            "consecutive_429": self.consecutive_429,
            "stop_reason": self.stop_reason,
            "methods": {name: dict(values) for name, values in self.methods.items()},
        }


def attach_accounting(target: Any, delta: RpcAccounting, attempted_at: datetime | None) -> None:
    target.rpc_calls = delta.attempts
    if hasattr(target, "rpc_successes"):
        target.rpc_successes = delta.successes
    if hasattr(target, "rpc_failures"):
        target.rpc_failures = delta.failures
    if hasattr(target, "rpc_retries"):
        target.rpc_retries = delta.retries
    if hasattr(target, "rpc_http_429"):
        target.rpc_http_429 = delta.http_429
    if hasattr(target, "rpc_timeouts"):
        target.rpc_timeouts = delta.timeouts
    if hasattr(target, "rpc_other_errors"):
        target.rpc_other_errors = delta.other_errors
    if hasattr(target, "rpc_cache_hits"):
        target.rpc_cache_hits = delta.cache_hits
    if hasattr(target, "rpc_accounting"):
        target.rpc_accounting = delta
    if hasattr(target, "attempted_at") and attempted_at is not None:
        target.attempted_at = attempted_at
    methods = delta.methods
    if hasattr(target, "largest_account_requests"):
        target.largest_account_requests = methods.get("getTokenLargestAccounts", {}).get("attempts", 0)
    if hasattr(target, "account_info_requests"):
        target.account_info_requests = methods.get("getAccountInfo", {}).get("attempts", 0)


def merge_accounting(summary: Any, delta: RpcAccounting) -> None:
    summary.rpc_calls = int(getattr(summary, "rpc_calls", 0)) + delta.attempts
    if hasattr(summary, "rpc_successes"):
        summary.rpc_successes += delta.successes
    if hasattr(summary, "rpc_failures"):
        summary.rpc_failures += delta.failures
    if hasattr(summary, "rpc_retries"):
        summary.rpc_retries += delta.retries
    if hasattr(summary, "rpc_http_429"):
        summary.rpc_http_429 += delta.http_429
    if hasattr(summary, "rpc_timeouts"):
        summary.rpc_timeouts += delta.timeouts
    if hasattr(summary, "rpc_other_errors"):
        summary.rpc_other_errors += delta.other_errors
    if hasattr(summary, "rpc_cache_hits"):
        summary.rpc_cache_hits += delta.cache_hits
    if hasattr(summary, "rpc_accounting"):
        current = summary.rpc_accounting
        if current is None:
            summary.rpc_accounting = delta.copy()
        else:
            summary.rpc_accounting = RpcAccounting(
                attempts=current.attempts + delta.attempts,
                successes=current.successes + delta.successes,
                failures=current.failures + delta.failures,
                retries=current.retries + delta.retries,
                http_429=current.http_429 + delta.http_429,
                timeouts=current.timeouts + delta.timeouts,
                other_errors=current.other_errors + delta.other_errors,
                cache_hits=current.cache_hits + delta.cache_hits,
                consecutive_429=delta.consecutive_429,
                stop_reason=delta.stop_reason or current.stop_reason,
                methods=_merge_methods(current.methods, delta.methods),
            )


def _merge_methods(
    left: dict[str, dict[str, int]],
    right: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    names = set(left) | set(right)
    merged: dict[str, dict[str, int]] = {}
    for name in names:
        a = left.get(name, {})
        b = right.get(name, {})
        merged[name] = {
            field_name: int(a.get(field_name, 0)) + int(b.get(field_name, 0))
            for field_name in METHOD_FIELDS
        }
    return merged


def accounting_payload(summary: Any) -> dict[str, Any]:
    accounting = getattr(summary, "rpc_accounting", None)
    payload = accounting.as_dict() if isinstance(accounting, RpcAccounting) else {
        "attempts": getattr(summary, "rpc_calls", 0),
        "successes": getattr(summary, "rpc_successes", 0),
        "failures": getattr(summary, "rpc_failures", 0),
        "retries": getattr(summary, "rpc_retries", 0),
        "http_429": getattr(summary, "rpc_http_429", 0),
        "timeouts": getattr(summary, "rpc_timeouts", 0),
        "other_errors": getattr(summary, "rpc_other_errors", 0),
        "cache_hits": getattr(summary, "rpc_cache_hits", 0),
    }
    payload["stop_reason"] = getattr(summary, "stop_reason", None) or payload.get("stop_reason")
    payload["stop_token"] = getattr(summary, "stop_token", None)
    return payload


def mark_batch_stop(summary: Any, token_address: str, reason: str = KIND_RATE_LIMITED) -> None:
    summary.status = "partial"
    summary.stop_reason = reason
    summary.stop_token = token_address
    summary.errors.append(
        {
            "token_address": token_address,
            "detail": "Repeated HTTP 429 from Solana RPC. Batch stopped without further tokens.",
            "stop_reason": reason,
        }
    )
