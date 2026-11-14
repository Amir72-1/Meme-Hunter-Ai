"""Process-wide shared Solana RPC client for creator evidence batches."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.services.rpc_accounting import KIND_CLIENT
from app.services.rpc_cooldown import cooldown_payload
from app.services.solana_rpc import SolanaRpcClient, SolanaRpcError

_manager: SharedRpcSession | None = None


class SharedRpcSession:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._client: SolanaRpcClient | None = None
        self._closed = False
        self._busy = False
        self.default_transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def client(self) -> SolanaRpcClient | None:
        return self._client

    async def try_begin_work(self) -> bool:
        async with self._lock:
            if self._closed or self._busy:
                return False
            self._busy = True
            return True

    async def end_work(self) -> None:
        async with self._lock:
            self._busy = False

    async def get_client(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> SolanaRpcClient:
        async with self._lock:
            if self._closed:
                raise SolanaRpcError("Shared Solana RPC session is shut down", kind=KIND_CLIENT)
            if self._client is not None and self._client.is_open:
                return self._client
            chosen = transport if transport is not None else self.default_transport
            client = SolanaRpcClient(settings=settings or get_settings(), transport=chosen)
            await client.__aenter__()
            self._client = client
            return client

    async def shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            self._busy = False
            client = self._client
            self._client = None
        if client is not None:
            await client.__aexit__(None, None, None)


def get_shared_rpc_session() -> SharedRpcSession:
    global _manager
    if _manager is None:
        _manager = SharedRpcSession()
    return _manager


def reset_shared_rpc_session() -> SharedRpcSession:
    global _manager
    _manager = SharedRpcSession()
    return _manager


async def shutdown_shared_rpc_session() -> None:
    global _manager
    if _manager is None:
        return
    await _manager.shutdown()
    _manager = None


@dataclass
class EvidenceGate:
    kind: str
    rpc: SolanaRpcClient | None = None
    cooldown: dict[str, Any] | None = None


@asynccontextmanager
async def evidence_rpc_gate(settings: Settings | None = None) -> AsyncIterator[EvidenceGate]:
    settings = settings or get_settings()
    manager = get_shared_rpc_session()
    if manager.closed:
        yield EvidenceGate(kind="shutdown")
        return
    started = await manager.try_begin_work()
    if not started:
        yield EvidenceGate(kind="conflict")
        return
    try:
        cooldown = cooldown_payload()
        if cooldown.get("active"):
            yield EvidenceGate(kind="cooldown", cooldown=cooldown)
            return
        rpc = await manager.get_client(settings)
        if rpc.cooldown_active():
            yield EvidenceGate(kind="cooldown", cooldown=cooldown_payload())
            return
        rpc.resume_after_cooldown()
        yield EvidenceGate(kind="ok", rpc=rpc)
    finally:
        await manager.end_work()
